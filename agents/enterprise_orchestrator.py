# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Enterprise Orchestrator - LangChain + Agent Card Discovery

The enterprise's internal orchestrator built with LangChain that DISCOVERS
ISV agents via their A2A Agent Cards and lets the LLM decide which agent
to route requests to based on skill descriptions.

This is the realistic enterprise pattern:
1. ISV publishes an Agent Card at /.well-known/agent-card.json
2. Enterprise orchestrator fetches cards at startup
3. Each agent skill becomes a LangChain tool (with description from the card)
4. LLM picks the right agent/skill based on the user's question
5. Tool executor sends A2A JSON-RPC to the agent URL from the card
6. LLM summarizes the response for internal stakeholders

Key design decisions:
- Uses LangChain (NOT Strands) - proves framework interoperability
- Uses raw httpx for A2A calls - no vendor SDK lock-in
- Agent Card drives discovery - the orchestrator doesn't hardcode what agents can do
- LLM decides routing - not if/else code
- The card's `url` is pinned to the URL the card was fetched from, so a card
  can't redirect the bearer token to another host or path
- A card URL must be an Amazon Bedrock AgentCore runtime card URL in a supported
  Region before any token is requested for it, and a fetched card must declare
  this enterprise's own token endpoint and scope, or it's dropped

The OAuth client is issued by the enterprise's own Amazon Cognito user pool and
registered with the SaaS provider at onboarding (the SaaS runtime's JWT
authorizer accepts only that client). No credential is shared between accounts.

Environment (all non-secret, set by the ECS task definition):
- AGENT_CARD_URLS: JSON array of Agent Card URLs provided by the SaaS provider
- COGNITO_SECRET_ARN: Secrets Manager secret holding this enterprise's own OAuth
  client from its own user pool (the value is never in the environment)
- BIND_HOST: listen address, loopback (127.0.0.1) by default; the container
  image sets it to all interfaces for container networking
- GUARDRAIL_ID, GUARDRAIL_VERSION: this account's Amazon Bedrock guardrail (its ID, never an ARN) and a
  numbered version; without both, the agent makes no model call

Deploy: ECS Fargate, behind an internal NLB reached from Amazon API Gateway
(HTTPS, JWT authorizer) through a VPC link. Users submit a question with
POST /jobs and poll GET /jobs/{job_id}.
"""

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

import boto3
import httpx
import uvicorn
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langchain.agents import create_agent
from langchain_aws import ChatBedrock
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("enterprise-orchestrator")

# Small AWS calls: the SDK's in-Region connect default, a short read, standard retries, 3 attempts in all.
AWS_CLIENT_CONFIG = Config(connect_timeout=1.1, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})
# InvokeModel returns the whole answer at once, so the read covers 4,096 output tokens; standard retries,
# 6 attempts in all (5 retries), with jittered backoff that rides out a short throttle.
BEDROCK_CLIENT_CONFIG = Config(connect_timeout=1.1, read_timeout=80, retries={"mode": "standard", "total_max_attempts": 6})


def _log_sdk_retries(client):
    """Log one INFO line per SDK retry: service, operation, attempt, and the error code or class."""
    service = client.meta.service_model.service_id.hyphenize()
    attempts = client.meta.config.retries.get("total_max_attempts")

    def note(request_dict=None, response=None, caught_exception=None, **_):
        if request_dict is not None:
            if caught_exception is not None:
                request_dict["context"]["sdk_last_error"] = type(caught_exception).__name__
            elif response is not None and response[0].status_code >= 400:
                code = (response[1].get("Error") or {}).get("Code")
                request_dict["context"]["sdk_last_error"] = code or f"HTTP {response[0].status_code}"
        return None  # botocore's retry handler decides

    def log_retry(request=None, event_name="", **_):
        ctx = getattr(request, "context", None) or {}
        attempt = (ctx.get("retries") or {}).get("attempt", 1)
        if attempt > 1:
            logger.info("SDK retry: service=%s operation=%s attempt=%d of %s after %s", service,
                        event_name.rsplit(".", 1)[-1], attempt, attempts, ctx.get("sdk_last_error", "-"))

    client.meta.events.register(f"needs-retry.{service}", note)
    client.meta.events.register(f"before-send.{service}", log_retry)


# -- Configuration --
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
COGNITO_SECRET_ARN = os.environ.get("COGNITO_SECRET_ARN", "")

# Agent Card URLs - provided by ISVs during onboarding.
# Each URL points to an A2A Agent Card (/.well-known/agent-card.json).
# In production this could come from SSM Parameter Store, a config file, or a service catalog.
def _parse_card_urls(raw: str) -> list[str]:
    """Parse AGENT_CARD_URLS as a JSON list of strings. Anything else gives [] (no discovery)."""
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, list) or not all(isinstance(u, str) for u in value):
        logger.error("AGENT_CARD_URLS must be a JSON list of strings: Agent Card discovery is disabled")
        return []
    return value


AGENT_CARD_URLS = _parse_card_urls(os.environ.get("AGENT_CARD_URLS", "[]"))

# Loopback by default. The container image sets BIND_HOST to all interfaces so
# the NLB can reach the task; the task's security group admits only the NLB.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = 8000

# Set by an API Gateway parameter mapping from the validated JWT's client_id
# claim (overwrite:header.x-a2a-caller-client-id = $context.authorizer.claims.client_id).
# "overwrite" replaces any value a client sends, so the header can't be spoofed
# through the API.
CALLER_CLIENT_ID_HEADER = "x-a2a-caller-client-id"

_WELL_KNOWN_CARD_SUFFIX = "/.well-known/agent-card.json"

_SUPPORTED_REGIONS = r"(us-east-1|us-east-2|us-west-2)"
# The SaasAgentCardUrl pattern from enterprise-services.yaml: an AgentCore runtime
# card URL in a supported Region, with no slash, query, fragment, whitespace,
# quote, or backslash in the runtime segment.
_CARD_URL_RE = re.compile(
    rf'https://bedrock-agentcore\.{_SUPPORTED_REGIONS}\.amazonaws\.com/runtimes/[^/?#\s"\\]+'
    r"/invocations/\.well-known/agent-card\.json"
)
# An Amazon Cognito user pool domain token endpoint, in a supported Region.
_COGNITO_TOKEN_RE = re.compile(rf"https://[a-z0-9-]{{1,63}}\.auth\.{_SUPPORTED_REGIONS}\.amazoncognito\.com/oauth2/token")


# ================================================================
# OAuth - Client-credentials (M2M) token fetch via Secrets Manager
# ================================================================
# The agent authenticates machine-to-machine using the OAuth 2.0
# client-credentials grant: it POSTs to the Cognito token endpoint with HTTP
# Basic (client_id:client_secret) and grant_type=client_credentials, scoped by
# the custom resource-server scope. The resulting access token is sent as the
# Authorization: Bearer header on cross-boundary A2A calls.

# Refresh the cached token this many seconds before it actually expires.
_TOKEN_REFRESH_SKEW = 60

_REQUIRED_OAUTH_FIELDS = ("client_id", "client_secret", "token_endpoint", "scope")


class OAuthNotConfigured(RuntimeError):
    """No usable OAuth client configuration: no outbound call is made (fail closed)."""


def _secrets_client():
    """Secrets Manager client. A separate function so offline tests can replace it."""
    client = boto3.client("secretsmanager", region_name=AWS_REGION, config=AWS_CLIENT_CONFIG)
    _log_sdk_retries(client)
    return client


def _load_cognito_config() -> dict | None:
    """Load the OAuth client-credentials config from AWS Secrets Manager.

    Returns None when the ARN is unset, the secret can't be read, or a required
    field (including the scope) is missing or invalid. There is no default scope
    and no environment-variable fallback: without a valid config, no token is
    requested and no agent is called.
    """
    if not COGNITO_SECRET_ARN:
        logger.error("COGNITO_SECRET_ARN is not set: outbound A2A calls are disabled")
        return None
    try:
        secret = _secrets_client().get_secret_value(SecretId=COGNITO_SECRET_ARN)
        creds = json.loads(secret.get("SecretString") or "")
    except Exception as e:
        # Only the exception type: never the secret value.
        logger.error("Failed to load the OAuth client config from Secrets Manager: %s", type(e).__name__)
        return None
    if not isinstance(creds, dict):
        logger.error("The OAuth client config secret is not a JSON object: outbound A2A calls are disabled")
        return None

    missing = [k for k in _REQUIRED_OAUTH_FIELDS if not isinstance(creds.get(k), str) or not creds.get(k)]
    if missing:
        logger.error("The OAuth client config secret is missing %s: outbound A2A calls are disabled", ", ".join(missing))
        return None
    if not _COGNITO_TOKEN_RE.fullmatch(creds["token_endpoint"]):
        logger.error(
            "token_endpoint is not an Amazon Cognito .../oauth2/token URL in a supported Region: "
            "outbound A2A calls are disabled"
        )
        return None
    if "/" not in creds["scope"] or any(c.isspace() for c in creds["scope"]):
        logger.error("scope must be one resource-server/scope value: outbound A2A calls are disabled")
        return None
    logger.info("Loaded the OAuth client config from Secrets Manager (scope=%s)", creds["scope"])
    return {k: creds[k] for k in _REQUIRED_OAUTH_FIELDS}


cognito_config = _load_cognito_config()

# Module-level token cache: reused until the refresh-before-expiry window.
_token_cache: dict = {}  # "token" and "expiry" once a token has been fetched


def _fetch_token(force: bool = False) -> str:
    """Return a client-credentials access token, cached and refreshed before expiry.

    Raises OAuthNotConfigured when there is no valid config, and RuntimeError when the
    token request fails. It never returns an empty token, so no call goes out
    unauthenticated.
    """
    if not cognito_config:
        raise OAuthNotConfigured("no valid OAuth client config")

    now = time.monotonic()
    if not force and _token_cache.get("token") and now < _token_cache.get("expiry", 0.0) - _TOKEN_REFRESH_SKEW:
        return _token_cache["token"]

    client_id = cognito_config["client_id"]
    client_secret = cognito_config["client_secret"]
    token_endpoint = cognito_config["token_endpoint"]
    scope = cognito_config["scope"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    try:
        resp = httpx.post(
            token_endpoint,
            data={"grant_type": "client_credentials", "scope": scope},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
    except Exception as e:
        # Never log the response body or any sensitive value - only the exception type.
        logger.error("Cognito auth request failed: %s", type(e).__name__)
        raise

    request_id = resp.headers.get("x-amz-cognito-request-id", "-")
    if resp.status_code != 200:
        # Do NOT cache; log only the status code (the body can echo sensitive values).
        logger.error("Cognito auth endpoint returned HTTP %d (request_id=%s)", resp.status_code, request_id)
        raise RuntimeError(f"Cognito token endpoint returned HTTP {resp.status_code}")

    try:
        body = resp.json()
    except ValueError:
        body = {}
    access_token = body.get("access_token", "") if isinstance(body, dict) else ""
    if not access_token:
        logger.error("Cognito auth endpoint returned 200 with no access grant (request_id=%s)", request_id)
        raise RuntimeError("Cognito token endpoint returned no access_token")

    expires_in = body.get("expires_in", 3600)
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        expires_in = 3600
    _token_cache["token"] = access_token
    _token_cache["expiry"] = time.monotonic() + expires_in
    logger.info("Obtained Cognito access grant (valid %ss, request_id=%s)", expires_in, request_id)
    return access_token


def get_bearer_token() -> str:
    """Return a bearer token via the client-credentials grant. Raises if it can't get one."""
    return _fetch_token()


# ================================================================
# Amazon Bedrock Guardrails
# ================================================================
# Every model call carries this account's guardrail. GUARDRAIL_ID is the guardrail's ID (never an ARN, so it
# always means this account and Region) and GUARDRAIL_VERSION a numbered version (never DRAFT).

def _guardrail_settings():
    """Read GUARDRAIL_ID and GUARDRAIL_VERSION. Returns (id, version, configured); without both, no model call."""
    gid = os.environ.get("GUARDRAIL_ID", "")
    version = os.environ.get("GUARDRAIL_VERSION", "")
    ok = bool(re.fullmatch(r"[a-z0-9]{1,64}", gid) and re.fullmatch(r"[1-9][0-9]{0,7}", version))
    if ok:
        logger.info("Guardrail: id=%s version=%s", gid, version)
    else:
        logger.error("Guardrail not configured: GUARDRAIL_ID=%r GUARDRAIL_VERSION=%r (an ID and a numbered version "
                     "are required); no model call will be made", gid, version)
    return gid, version, ok


GUARDRAIL_ID, GUARDRAIL_VERSION, GUARDRAIL_CONFIGURED = _guardrail_settings()
GUARDRAIL_CONFIG = ({"guardrailIdentifier": GUARDRAIL_ID, "guardrailVersion": GUARDRAIL_VERSION, "trace": "enabled"}
                    if GUARDRAIL_CONFIGURED else None)

_POLICY_DIRECTIONS = {"input": "input", "inputAssessment": "input", "outputs": "output", "outputAssessments": "output"}
# These keys can hold the screened text itself (a guardrail trace is not masked), so they are never read.
_POLICY_UNREAD = frozenset({"modelOutput", "match", "regex", "text"})


def policy_summary(obj, direction=None) -> list[str]:
    """The guardrail policies that acted, as "<input|output>:<type>:<action>[:<confidence>]", at most 10.

    Walks a guardrail trace or assessment. An entry is a dict whose action isn't NONE, or that has
    detected: true. It never reads modelOutput, match, regex, or text, so the result is safe to log.
    Any error gives [].
    """
    out = []

    def walk(node, where):
        if len(out) >= 10:
            return
        if isinstance(node, dict):
            action = node.get("action")
            if ("action" in node and action != "NONE") or node.get("detected") is True:
                entry = f"{where or 'unknown'}:{node.get('type') or node.get('name') or '-'}:"
                entry += str(action) if action and action != "NONE" else "DETECTED"
                if node.get("confidence"):
                    entry += f":{node['confidence']}"
                out.append(entry)
                return
            for key, value in node.items():
                if key not in _POLICY_UNREAD:
                    walk(value, _POLICY_DIRECTIONS.get(key, where))
        elif isinstance(node, list):
            for item in node:
                walk(item, where)

    try:
        walk(obj, direction)
    except Exception:
        return []
    return out[:10]


class GuardrailBlocked(Exception):
    """The guardrail intervened. The message holds the stage, ref, and policy entries only, never the text."""

    def __init__(self, stage, policies, ref="-"):
        self.stage, self.policies, self.ref = stage, list(policies), ref
        super().__init__(f"guardrail intervened: stage={stage} ref={ref} policies={self.policies}")


class GuardrailUnavailable(Exception):
    """The guardrail check couldn't run: not configured, an error after the SDK's retries, or oversize text."""

    def __init__(self, stage, reason, ref="-"):
        self.stage, self.reason, self.ref = stage, reason, ref
        super().__init__(f"guardrail check failed: stage={stage} ref={ref} reason={reason}")


# Text from the other organization that a model call can't evaluate (a tool result, an Agent Card) is
# screened with ApplyGuardrail (source INPUT) before any model sees it.
GUARDRAIL_MAX_SCREEN_CHARS = 100_000  # at most 100 text units per check
_guardrail_lock = threading.Lock()
_guardrail_clients: dict = {}


def _guardrail_client():
    """The bedrock-runtime client for ApplyGuardrail, created once. A function, so offline tests can replace it."""
    with _guardrail_lock:
        if "client" not in _guardrail_clients:
            client = boto3.client("bedrock-runtime", region_name=AWS_REGION, config=AWS_CLIENT_CONFIG)
            _log_sdk_retries(client)
            _guardrail_clients["client"] = client
        return _guardrail_clients["client"]


def screen_text(text, stage, ref="-") -> None:
    """Screen text with ApplyGuardrail (source INPUT) before any model sees it.

    Raises GuardrailBlocked when the guardrail intervenes and GuardrailUnavailable when the check can't run
    (fail closed). Logs nothing: the callers log.
    """
    if not GUARDRAIL_CONFIGURED:
        raise GuardrailUnavailable(stage, "not configured", ref)
    if not text or not text.strip():
        return
    if len(text) > GUARDRAIL_MAX_SCREEN_CHARS:
        raise GuardrailUnavailable(stage, f"text over {GUARDRAIL_MAX_SCREEN_CHARS} characters", ref)
    try:
        resp = _guardrail_client().apply_guardrail(
            guardrailIdentifier=GUARDRAIL_ID,
            guardrailVersion=GUARDRAIL_VERSION,
            source="INPUT",
            content=[{"text": {"text": text}}],
        )
    except ClientError as e:
        raise GuardrailUnavailable(stage, e.response.get("Error", {}).get("Code") or "ClientError", ref) from None
    except BotoCoreError as e:
        raise GuardrailUnavailable(stage, type(e).__name__, ref) from None
    action = resp.get("action")
    if action == "GUARDRAIL_INTERVENED":
        raise GuardrailBlocked(stage, policy_summary(resp.get("assessments"), "input"), ref)
    if action != "NONE":
        raise GuardrailUnavailable(stage, f"unexpected action {action!r}", ref)


def card_screen_text(card):
    """The Agent Card text the model reads as tool text: the name and description, and each named skill's name,
    description, and first three examples. Missing and blank values are skipped."""
    parts = [card.get("name"), card.get("description")]
    skills = card.get("skills")
    for skill in skills if isinstance(skills, list) else []:
        if isinstance(skill, dict) and skill.get("name"):
            parts += [skill.get("name"), skill.get("description")]
            examples = skill.get("examples")
            if isinstance(examples, list):
                parts += examples[:3]
    return "\n".join(str(p) for p in parts if p is not None and str(p).strip())


# The InvokeModel body field the user guide asks for with a guardrail. The suffix is random per process; it is
# never logged.
GUARDRAIL_TAG_SUFFIX = secrets.token_hex(8)


# ================================================================
# Agent Card Discovery - fetch ISV Agent Cards and register skills
# ================================================================

def pinned_agent_url(card_url: str, card_agent_url) -> str | None:
    """Return the URL to send A2A messages to, or None if the card's `url` isn't pinned.

    The card's `url` must equal the card URL's base (the card URL minus
    /.well-known/agent-card.json). Scheme and host (with port) must match exactly
    (case-insensitive); paths are compared after percent-decoding and with trailing
    slashes ignored, so `arn%3Aaws%3A...%2Fname` and `arn:aws:.../name` are the same
    path. The query must match exactly and neither URL may have a fragment. The
    returned URL is the card URL's base with a trailing slash: the address the
    card was actually fetched from.
    """
    if not isinstance(card_url, str) or not isinstance(card_agent_url, str) or not card_agent_url:
        return None
    card = urlsplit(card_url)
    agent = urlsplit(card_agent_url)
    if card.scheme.lower() != "https" or not card.netloc or not card.path.endswith(_WELL_KNOWN_CARD_SUFFIX):
        return None
    base_path = card.path[: -len(_WELL_KNOWN_CARD_SUFFIX)]
    if agent.scheme.lower() != card.scheme.lower() or agent.netloc.lower() != card.netloc.lower():
        return None
    if unquote(agent.path).rstrip("/") != unquote(base_path).rstrip("/"):
        return None
    if agent.query != card.query or card.fragment or agent.fragment:
        return None
    # urlsplit doesn't decode, so the base keeps the card URL's own percent-encoding.
    return urlunsplit((card.scheme, card.netloc, base_path.rstrip("/") + "/", card.query, ""))


def _valid_card_url(url) -> bool:
    """True if url is an AgentCore runtime Agent Card URL in a supported Region (checked before any token)."""
    return isinstance(url, str) and _CARD_URL_RE.fullmatch(url) is not None


def _card_declares_our_auth(card: dict) -> bool:
    """True if the card has an OAuth 2.0 client-credentials scheme at our token endpoint with our scope."""
    if not cognito_config:
        return False
    schemes = card.get("securitySchemes")
    if not isinstance(schemes, dict):
        return False
    for scheme in schemes.values():
        if not isinstance(scheme, dict) or scheme.get("type") != "oauth2":
            continue
        flows = scheme.get("flows")
        flow = flows.get("clientCredentials") if isinstance(flows, dict) else None
        if not isinstance(flow, dict):
            continue
        scopes = flow.get("scopes")
        if (flow.get("tokenUrl") == cognito_config["token_endpoint"]
                and isinstance(scopes, dict) and cognito_config["scope"] in scopes):
            return True
    return False


def fetch_agent_card(card_url: str) -> dict | None:
    """Fetch an A2A Agent Card from its well-known URL (refreshes token on a 401)."""
    def _get(token: str) -> dict:
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=15) as client:
            resp = client.get(card_url, headers=headers)
            logger.info(
                "Agent Card fetch: url=%s status=%d request_id=%s",
                card_url, resp.status_code, resp.headers.get("x-amzn-requestid", "-"),
            )
            resp.raise_for_status()
            return resp.json()

    try:
        token = get_bearer_token()
        try:
            return _get(token)
        except httpx.HTTPStatusError as e:
            # On a 401, force one token refresh and retry once.
            if e.response.status_code == 401:
                return _get(_fetch_token(force=True))
            raise
    except Exception as e:
        logger.warning("Failed to fetch Agent Card from %s: %s", card_url, type(e).__name__)
        return None


def discover_agents() -> list[dict]:
    """Discover all available agents by fetching their Agent Cards.

    Each Agent Card tells us:
    - name: who this agent is
    - description: what it does (high level)
    - url: where to send A2A messages (must be pinned to the card URL)
    - skills: specific capabilities with descriptions and examples
    - securitySchemes: how to get credentials (must name this enterprise's
      own token endpoint and scope)
    """
    agents = []
    for card_url in AGENT_CARD_URLS:
        # Checked before any token is requested or any request is sent.
        if not _valid_card_url(card_url):
            logger.error("Skipped Agent Card URL %r: not an AgentCore runtime card URL in a supported Region", card_url)
            continue
        card = fetch_agent_card(card_url)
        if not isinstance(card, dict):
            continue

        # Send the bearer token only to the address the card was fetched from.
        agent_url = pinned_agent_url(card_url, card.get("url"))
        if agent_url is None:
            logger.error(
                "Rejected Agent Card from %s: its url %r doesn't match the card URL", card_url, card.get("url"),
            )
            continue
        if not _card_declares_our_auth(card):
            logger.error(
                "Rejected Agent Card from %s: its securitySchemes don't name this enterprise's token endpoint and scope",
                card_url,
            )
            continue
        # The card's text becomes tool text, which InvokeModel's prompt-attack filter doesn't evaluate.
        try:
            screen_text(card_screen_text(card), "agent_card", card_url)
        except GuardrailBlocked as e:
            logger.warning("Rejected Agent Card from %s: the guardrail blocked its text (policies=%s)", card_url, e.policies)
            continue
        except GuardrailUnavailable as e:
            logger.error("Rejected Agent Card from %s: guardrail check failed (%s)", card_url, e.reason)
            continue

        skills = [s for s in card.get("skills") or [] if isinstance(s, dict) and s.get("name")]
        agents.append({
            "name": card.get("name", "Unknown Agent"),
            "description": card.get("description", ""),
            "url": agent_url,
            "protocol": card.get("protocol", "a2a"),
            "skills": skills,
            "capabilities": card.get("capabilities", {}),
        })
        logger.info(
            "Discovered agent '%s' at %s with %d skills: %s",
            card.get("name"),
            agent_url,
            len(skills),
            [s["name"] for s in skills],
        )

    return agents


logger.info("Discovering ISV agents from %d Agent Card URL(s)...", len(AGENT_CARD_URLS))
discovered_agents = discover_agents()
logger.info("Discovery complete: %d agent(s) available", len(discovered_agents))


# ================================================================
# A2A Client - Raw JSON-RPC (no Strands SDK, no vendor lock-in)
# ================================================================

# The SaaS run has 300 seconds (its model calls and the callback fit inside it), and the reply comes only at
# the end. A connection gets 5 seconds. The call is never repeated, except once after a 401.
_A2A_TIMEOUT = httpx.Timeout(300.0, connect=5.0)


def send_a2a_message(agent_url: str, message_text: str) -> str:
    """Send a message to an A2A agent via JSON-RPC.

    This is the A2A protocol in its simplest form:
    - POST to the agent URL
    - Body is JSON-RPC with method "message/send"
    - Auth via OAuth bearer token
    - Response contains artifacts with text parts
    """
    rpc_id = f"lc-{uuid4().hex}"
    payload = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
                "messageId": uuid4().hex,
            }
        },
    }
    def _post(token: str, attempt: int) -> httpx.Response:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=_A2A_TIMEOUT) as client:
            resp = client.post(agent_url, json=payload, headers=headers)
            # Log the IDs each side can look up in its own logs.
            logger.info(
                "A2A response: rpc_id=%s attempt=%d status=%d request_id=%s",
                rpc_id, attempt, resp.status_code, resp.headers.get("x-amzn-requestid", "-"),
            )
            resp.raise_for_status()
            return resp

    token = get_bearer_token()
    try:
        resp = _post(token, 1)
    except httpx.HTTPStatusError as e:
        # On a 401, force one token refresh and retry once.
        if e.response.status_code == 401:
            resp = _post(_fetch_token(force=True), 2)
        else:
            raise

    try:
        body = resp.json()
    except ValueError:
        raise RuntimeError(f"A2A reply is not JSON (rpc_id={rpc_id})") from None
    if not isinstance(body, dict):
        raise RuntimeError(f"A2A reply is not a JSON-RPC object (rpc_id={rpc_id})")

    error = body.get("error")
    if error is not None:
        err = error if isinstance(error, dict) else {}
        message = str(err.get("message", ""))[:500]
        logger.error("A2A JSON-RPC error: rpc_id=%s code=%s message=%r", rpc_id, err.get("code"), message)
        raise RuntimeError(f"A2A agent returned JSON-RPC error {err.get('code')}: {message} (rpc_id={rpc_id})")

    result = body.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"A2A reply has no result (rpc_id={rpc_id})")
    task_id = result.get("id") if result.get("kind") == "task" else result.get("taskId")
    logger.info(
        "A2A result: rpc_id=%s task_id=%s state=%s",
        rpc_id, task_id or "-", (result.get("status") or {}).get("state", "-"),
    )
    # Fail closed: a task that didn't complete (for example, one that failed after its model retries ran
    # out) never reaches the model as a tool result. A message result has no state and is accepted.
    if result.get("kind") == "task":
        state = (result.get("status") or {}).get("state")
        if state != "completed":
            logger.error("A2A task did not complete: rpc_id=%s task_id=%s state=%s", rpc_id, task_id or "-", state)
            raise RuntimeError(f"A2A task did not complete (state={state}, rpc_id={rpc_id})")

    # Extract text from A2A response (check artifacts first, then history)
    texts = []
    for artifact in result.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if part.get("kind") == "text":
                texts.append(part.get("text", ""))
    if not texts:
        for msg in result.get("history") or []:
            if msg.get("role") == "agent":
                for part in msg.get("parts") or []:
                    if part.get("kind") == "text":
                        texts.append(part.get("text", ""))

    # The reply reaches the model as a tool result, which InvokeModel's prompt-attack filter doesn't evaluate.
    reply = "".join(texts) if texts else json.dumps(result, indent=2)
    screen_text(reply, "a2a_reply", rpc_id)
    return reply


# ================================================================
# Build LangChain Tools from Agent Card Skills
# ================================================================

def _tool_name_part(text: str) -> str:
    """Reduce card text to the characters Bedrock allows in a tool name (letters, digits, _)."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(text)).strip("_") or "agent"


def build_langchain_tools(agents: list[dict]) -> list:
    """Create a LangChain tool for each skill in each discovered agent.

    The Agent Card's skill descriptions become the tool descriptions
    that the LLM uses to decide which agent to call. This is the key
    mechanism: the ISV writes good skill descriptions, and the enterprise
    LLM uses them for routing - no hardcoded if/else logic needed.
    """
    tools = []

    for agent in agents:
        agent_url = agent["url"]
        agent_name = _tool_name_part(agent["name"])

        for skill in agent["skills"]:
            skill_name = _tool_name_part(skill["name"])
            skill_description = skill.get("description", "")
            examples = skill.get("examples", [])

            # Build a descriptive tool name and description
            tool_name = f"{agent_name}__{skill_name}"[:64]
            tool_description = (
                f"{skill_description} "
                f"(External agent: {agent['name']})"
            )
            if examples:
                tool_description += f" Examples: {'; '.join(examples[:3])}"

            # Create a closure to capture agent_url for this specific tool
            def make_tool(name: str, desc: str, url: str):
                def call_external_agent(question: str) -> str:
                    """Send a natural language question to an external agent via A2A protocol."""
                    return send_a2a_message(url, question)
                call_external_agent.__name__ = name
                call_external_agent.__doc__ = desc
                return langchain_tool(call_external_agent)

            tools.append(make_tool(tool_name, tool_description, agent_url))

    # If no skills were found in cards, create a single catch-all tool per agent
    for agent in agents:
        if not agent["skills"]:
            agent_name = _tool_name_part(agent["name"])

            def make_fallback_tool(name: str, desc: str, url: str):
                def call_agent(question: str) -> str:
                    """Send a natural language question to an external agent via A2A protocol."""
                    return send_a2a_message(url, question)
                call_agent.__name__ = name
                call_agent.__doc__ = desc
                return langchain_tool(call_agent)

            tools.append(make_fallback_tool(
                f"{agent_name}__ask",
                f"{agent['description']} (External agent: {agent['name']})",
                agent["url"],
            ))

    return tools


agent_tools = build_langchain_tools(discovered_agents)
logger.info("Built %d LangChain tools from Agent Cards:", len(agent_tools))
for t in agent_tools:
    logger.info("  - %s: %s", t.name, t.description[:100])


# ================================================================
# LangChain Agent - tool-calling with Bedrock
# ================================================================

# ChatBedrock sends Claude requests through InvokeModel, where langchain-aws caps
# the reply at 1,024 output tokens unless max_tokens is set. That cut the final
# answer off mid-sentence, so set the limit explicitly.
llm = ChatBedrock(
    model_id="us.anthropic.claude-sonnet-4-6",
    region_name=AWS_REGION,
    max_tokens=4096,
    config=BEDROCK_CLIENT_CONFIG,
    guardrails=GUARDRAIL_CONFIG,
    # The user guide requires this body field with a guardrail; no input tags are used, so the guardrail
    # reads the whole prompt. The suffix is per process, not per request as the docs recommend, because
    # model_kwargs is fixed; it is never logged.
    model_kwargs={"amazon-bedrock-guardrailConfig": {"tagSuffix": GUARDRAIL_TAG_SUFFIX}} if GUARDRAIL_CONFIGURED else {},
)
_log_sdk_retries(llm.client)

SYSTEM_PROMPT = """You are an Enterprise Operations Orchestrator for a large organization.
You have access to external specialist agents discovered via A2A Agent Cards.

Each tool represents a capability from an external SaaS agent. The tool descriptions
tell you what each agent can do; use them to decide which tool to call.

When a user asks a question:
1. Choose the most appropriate tool based on the question and tool descriptions
2. Send the user's question (or a refined version) to the external agent
3. Summarize the agent's response clearly for internal stakeholders:
   - Highlight critical items that need immediate attention
   - Include escalation contacts when available
   - Frame results in business terms; your audience is leadership
   - Suggest next steps (remediation priorities, escalation, etc.)
   - Include risk ratings and estimated effort when available

If no tool matches the question, say so; don't force a tool call.
If the agent's response is already clear and complete, present it directly
without unnecessary summarization.
Report only what the scan results and the enterprise context show. Don't name laws, regulations, certifications, or industry standards.
Write plain text only: no emoji or other pictographic symbols, such as check marks, warning signs, or colored circles."""

# create_agent builds the tool-calling loop: the model sees the system prompt
# and the tools built from the Agent Cards, calls a tool when it needs one, and
# gets the result back until it can give a final answer.
agent_executor = create_agent(llm, agent_tools, system_prompt=SYSTEM_PROMPT)

# Allow at most 5 tool-calling rounds. Each round is a model step plus a tool
# step, and the final answer is one more step: recursion_limit = 2 * 5 + 1.
AGENT_RECURSION_LIMIT = 2 * 5 + 1


# ================================================================
# REST API (FastAPI)
# ================================================================

# No interactive docs or schema: the API is reached only through API Gateway
# routes, and nothing here needs to describe itself to callers.
app = FastAPI(
    title="Enterprise Orchestrator (LangChain + A2A Agent Cards)",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

MAX_QUESTION_CHARS = 4000

# Returned to the caller when a job fails; the detail goes to the logs only.
JOB_FAILED_MESSAGE = "The orchestrator could not complete the request."
# Returned when this organization's guardrail blocks the question, a model call, or the other side's reply.
GUARDRAIL_BLOCKED_MESSAGE = "The request was blocked by this organization's Amazon Bedrock guardrail."

# Cognito app client IDs: word characters and "+", 1 to 128 characters.
_CLIENT_ID_PATTERN = re.compile(r"[\w+]{1,128}", re.ASCII)


class JobRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    # A fixed message: don't echo the caller's input back.
    return JSONResponse(
        status_code=422,
        content={"detail": f"The body must be JSON with a question of 1 to {MAX_QUESTION_CHARS:,} characters."},
    )


def caller_client_id(request: Request) -> str:
    """The caller's app client ID, set by API Gateway from the validated JWT.

    Fails closed: a request without a well-formed value (for example, one that
    didn't come through the API Gateway mapping) is rejected.
    """
    value = request.headers.get(CALLER_CLIENT_ID_HEADER, "")
    if not _CLIENT_ID_PATTERN.fullmatch(value):
        logger.warning("Rejected a jobs request without a valid %s header", CALLER_CLIENT_ID_HEADER)
        raise HTTPException(status_code=403, detail="Forbidden")
    return value


# -- Jobs (submit, then poll) --
# API Gateway HTTP APIs end an integration call after 30 s, and an orchestrated
# answer takes longer, so a question runs as a background job.
#
# Single-task assumption: jobs live in this process's memory, which works because
# the ECS service runs one task (DesiredCount: 1). Jobs are lost on restart. With
# more than one task, keep jobs in a shared store such as Amazon DynamoDB.
MAX_JOBS = 100
JOB_TTL_SECONDS = 3600

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _prune_jobs() -> None:
    """Drop finished jobs older than the TTL, then the oldest finished jobs while over the bound.

    Call with _jobs_lock held. Queued and running jobs are never dropped.
    """
    now = time.monotonic()
    finished = [(j["created"], jid) for jid, j in _jobs.items() if j["status"] in ("succeeded", "failed")]
    for created, jid in finished:
        if now - created > JOB_TTL_SECONDS:
            del _jobs[jid]
    for _, jid in sorted(c for c in finished if c[1] in _jobs):
        if len(_jobs) < MAX_JOBS:
            break
        del _jobs[jid]


class _GuardrailWatch(BaseCallbackHandler):
    """Notes a guardrail intervention on an InvokeModel call. langchain-aws reports it through on_llm_error, with
    reason GUARDRAIL_INTERVENED and the trace, and returns the guardrail's blocked text as the answer."""

    def __init__(self):
        self.intervened = False
        self.policies = []

    def on_llm_error(self, error, **kwargs):
        if kwargs.get("reason") == "GUARDRAIL_INTERVENED":
            self.intervened = True
            self.policies += policy_summary(kwargs.get("trace"))


def _ask(question: str) -> str:
    """Run the LangChain agent on one question and return its final text.

    The LLM will:
    1. Read the tool descriptions (derived from Agent Card skills)
    2. Pick the best agent/skill to handle the question
    3. Send an A2A message to that agent
    4. Summarize the response for internal stakeholders
    """
    if not GUARDRAIL_CONFIGURED:
        raise GuardrailUnavailable("model", "not configured")
    watch = _GuardrailWatch()
    result = agent_executor.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": AGENT_RECURSION_LIMIT, "callbacks": [watch]},
    )
    if watch.intervened:
        raise GuardrailBlocked("model", watch.policies[:10])
    output = result["messages"][-1].content
    # LangChain may return a list of content blocks instead of a string
    if isinstance(output, list):
        output = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in output
        )
    return str(output)


def _run_job(job_id: str, question: str) -> None:
    with _jobs_lock:
        _jobs[job_id]["status"] = "running"
    logger.info("Job running: job_id=%s", job_id)
    try:
        # The user's question is screened for prompt attacks: InvokeModel applies that filter only to tagged input.
        screen_text(question, "question", job_id)
        response = _ask(question)
    except GuardrailBlocked as e:
        logger.warning("Guardrail intervened: job_id=%s stage=%s ref=%s policies=%s", job_id, e.stage, e.ref, e.policies)
        with _jobs_lock:
            _jobs[job_id].update(status="failed", error=GUARDRAIL_BLOCKED_MESSAGE, finished_at=_now_iso())
        return
    except Exception:
        # The detail stays in the logs; the caller sees a generic error.
        logger.exception("Job failed: job_id=%s", job_id)
        with _jobs_lock:
            _jobs[job_id].update(status="failed", error=JOB_FAILED_MESSAGE, finished_at=_now_iso())
        return
    with _jobs_lock:
        _jobs[job_id].update(status="succeeded", response=response, finished_at=_now_iso())
    logger.info("Job succeeded: job_id=%s chars=%d", job_id, len(response))


@app.post("/jobs", status_code=202)
def submit_job(body: JobRequest, client_id: str = Depends(caller_client_id)):
    """Queue a question for the orchestrator. Returns a job ID to poll with GET /jobs/{job_id}."""
    job_id = str(uuid4())
    with _jobs_lock:
        _prune_jobs()
        if len(_jobs) >= MAX_JOBS:
            logger.warning("Rejected a job: %d jobs are queued or running", len(_jobs))
            raise HTTPException(status_code=503, detail="Too many jobs in progress. Try again later.")
        _jobs[job_id] = {
            "owner": client_id,
            "status": "queued",
            "response": None,
            "error": None,
            "submitted_at": _now_iso(),
            "finished_at": None,
            "created": time.monotonic(),
        }
    logger.info("Job queued: job_id=%s caller_client_id=%s question_chars=%d", job_id, client_id, len(body.question))
    _executor.submit(_run_job, job_id, body.question)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, client_id: str = Depends(caller_client_id)):
    """Return a job's status and, when it has finished, its response or a generic error."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        # Another caller's job gets the same 404 as an unknown one, so job IDs can't be probed.
        if job is None or job["owner"] != client_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "job_id": job_id,
            "status": job["status"],
            "response": job["response"],
            "error": job["error"],
            "submitted_at": job["submitted_at"],
            "finished_at": job["finished_at"],
        }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "agent": "Enterprise Orchestrator",
        "framework": "LangChain",
        "discovery": "A2A Agent Cards",
        "oauth": cognito_config is not None,
        "guardrail": {"id": GUARDRAIL_ID, "version": GUARDRAIL_VERSION} if GUARDRAIL_CONFIGURED else None,
        "discovered_agents": [
            {
                "name": a["name"],
                "skills": [s["name"] for s in a["skills"]],
                "url": a["url"],
            }
            for a in discovered_agents
        ],
        "total_tools": len(agent_tools),
    }


@app.get("/agents")
def list_agents():
    """List all discovered agents and their capabilities (from Agent Cards)."""
    return {
        "agents": [
            {
                "name": a["name"],
                "description": a["description"],
                "url": a["url"],
                "protocol": a["protocol"],
                "skills": a["skills"],
                "capabilities": a["capabilities"],
            }
            for a in discovered_agents
        ]
    }


if __name__ == "__main__":
    logger.info("Starting Enterprise Orchestrator (LangChain + Agent Card Discovery) on %s:%d", BIND_HOST, PORT)
    logger.info("Discovered %d agents with %d total skills", len(discovered_agents), len(agent_tools))
    uvicorn.run(app, host=BIND_HOST, port=PORT)
