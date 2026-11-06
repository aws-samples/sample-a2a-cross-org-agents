# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Compliance SaaS Agent - Provider Side

A SaaS compliance product exposed via A2A protocol on Amazon Bedrock AgentCore,
running in the SaaS provider's AWS account. Scans AWS accounts for compliance
violations and provides remediation guidance. Calls back to the customer's
Enterprise Context Agent (in the customer's AWS account) for account metadata
and escalation contacts.

Trust model (two-way workload identity federation, no shared secret):
- Inbound: the customer's orchestrator gets a token from the customer's own
  identity provider. The AgentCore JWT authorizer accepts only the discovery
  URL, client, and scope registered at onboarding.
- Outbound: this agent gets a token from the SaaS provider's own identity
  provider, with the SaaS callback client's own credentials, and sends it only
  to the callback URL the customer registered at onboarding. The customer's
  API Gateway JWT authorizer accepts only that client.

Before any callback token is requested, the agent fetches the customer's
public context Agent Card anonymously from the registered callback URL and
checks that the card is pinned to that URL and declares this SaaS provider's
token endpoint and callback scope. A verified card is reused for 300 seconds;
a failed check is never cached. Every failure gives the model one generic
message, and the detail goes to this account's logs only.

Environment (all non-secret, set with `agentcore launch --env`):
- CALLBACK_URL: the callback URL the customer registered at onboarding
  (https://<api-id>.execute-api.<region>.amazonaws.com/)
- CALLBACK_SECRET_ARN: Secrets Manager secret holding the SaaS callback
  client's own credentials (client ID and secret, token endpoint, scope)
- CARD_AUTH_URL, CARD_AUTH_SCOPE: the customer's token endpoint and the scope
  its orchestrator uses, declared in the Agent Card's securitySchemes
- BIND_HOST: listen address, loopback (127.0.0.1) by default; the container
  image sets it to all interfaces for container networking
- GUARDRAIL_ID, GUARDRAIL_VERSION: this account's Amazon Bedrock guardrail (its ID, never an ARN) and a
  numbered version; without both, the agent makes no model call

Deploy: agentcore configure -e compliance_saas_agent.py --protocol A2A
"""

import base64
import json
import logging
import os
import re
import threading
import time
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit
from uuid import uuid4

import boto3
import httpx
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    AgentCard,
    AgentSkill,
    ClientCredentialsOAuthFlow,
    ContentTypeNotSupportedError,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityScheme,
)
from a2a.utils.errors import ServerError
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI
from strands import Agent, ToolContext, tool
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent, HookProvider, HookRegistry
from strands.models.bedrock import BedrockModel
from strands.multiagent.a2a import A2AServer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("compliance-saas-agent")

# Small AWS calls: the SDK's in-Region connect default, a short read, standard retries, 3 attempts in all.
AWS_CLIENT_CONFIG = Config(connect_timeout=1.1, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})
# ConverseStream: the read bounds the first byte and each gap of the stream. 8 attempts ride out a short
# throttle (at most 71 seconds of jittered backoff) and still end inside the orchestrator's 300 seconds.
# The SDK retries only errors that arrive before the stream starts; Strands' own retry is off (retry_strategy).
BEDROCK_CLIENT_CONFIG = Config(connect_timeout=1.1, read_timeout=20, retries={"mode": "standard", "total_max_attempts": 8})


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
AGENT_URL = os.environ.get("AGENTCORE_RUNTIME_URL", "http://127.0.0.1:9000/")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
MODEL_ID = "us.anthropic.claude-sonnet-4-6"

# The callback URL the customer registered at onboarding. It's the only address
# the callback token is ever sent to, and it's checked once at import.
CALLBACK_URL = os.environ.get("CALLBACK_URL", "")

# Secrets Manager secret with the SaaS callback client's own credentials, written
# by the SaaS provider notebook. Its value is never baked into the image or passed
# as an environment variable; only the ARN is.
CALLBACK_SECRET_ARN = os.environ.get("CALLBACK_SECRET_ARN", "")

# Declared in the Agent Card so callers know how to authenticate: the customer's
# token endpoint and scope. The AgentCore JWT authorizer enforces them; the agent
# itself never sees an unauthenticated call.
CARD_AUTH_URL = os.environ.get("CARD_AUTH_URL", "")
CARD_AUTH_SCOPE = os.environ.get("CARD_AUTH_SCOPE", "")

# Loopback by default. The container image sets BIND_HOST to all interfaces so
# the AgentCore runtime can reach the port inside its microVM.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "9000"))

# A successful callback-config read is reused for this long. A failed read is
# never cached, so a new secret value takes effect on the next call.
CALLBACK_CONFIG_TTL = 300

# A verified context Agent Card is reused for this long. A failed check is never cached.
CONTEXT_CARD_TTL = 300

# The context Agent Card is read in chunks and refused past this size.
CARD_MAX_BYTES = 65536
_CARD_TIMEOUT = 10

# Refresh a cached token this many seconds before it actually expires.
_TOKEN_REFRESH_SKEW = 60

# The callback goes through an Amazon API Gateway HTTP API, which answers within its 30-second integration
# timeout: 35 seconds to read leaves 5 for TLS and the gateway. A connection gets 5 seconds.
_CALLBACK_TIMEOUT = httpx.Timeout(35.0, connect=5.0)

_WELL_KNOWN_CARD_SUFFIX = "/.well-known/agent-card.json"

_REQUIRED_CALLBACK_FIELDS = ("client_id", "client_secret", "token_endpoint", "scope")

_SUPPORTED_REGIONS = r"(us-east-1|us-east-2|us-west-2)"
# An API Gateway HTTP API default endpoint with path "/", in a supported Region.
_CALLBACK_URL_RE = re.compile(rf"https://[a-z0-9]{{10}}\.execute-api\.{_SUPPORTED_REGIONS}\.amazonaws\.com/")
# An Amazon Cognito user pool domain token endpoint, in a supported Region.
_COGNITO_TOKEN_RE = re.compile(rf"https://[a-z0-9-]{{1,63}}\.auth\.{_SUPPORTED_REGIONS}\.amazoncognito\.com/oauth2/token")
# One resource-server/scope value.
_SCOPE_RE = re.compile(r"[a-z0-9-]{1,64}/[a-z0-9-]{1,64}")

# The one message the model gets for every callback failure. The detail is logged.
CONTEXT_UNAVAILABLE_MESSAGE = (
    "Enterprise context unavailable. Continue the scan and say that escalation contacts could not be retrieved."
)


class CallbackError(Exception):
    """The callback to the customer's context agent failed. Details are logged, not returned."""


class CallbackUnavailable(CallbackError):
    """No usable callback: invalid callback URL, no stored credentials, or an unverified context card."""


def _is_https_url(value: str, path_suffix: str = "") -> bool:
    """True if value is an https URL with a host and, optionally, the given path suffix."""
    if not isinstance(value, str) or not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme == "https" and bool(parsed.netloc) and parsed.path.endswith(path_suffix)


def _is_registered_callback_url(value: str) -> bool:
    """True if value has the registered callback URL shape: https, an execute-api host, path "/", nothing else."""
    if not isinstance(value, str) or not _CALLBACK_URL_RE.fullmatch(value):
        return False
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and port is None
        and parts.path == "/"
        and not parts.query
        and not parts.fragment
    )


CALLBACK_URL_VALID = _is_registered_callback_url(CALLBACK_URL)
if not CALLBACK_URL_VALID:
    # Fail closed: the agent still serves scans, and every callback attempt is refused.
    logger.error("CALLBACK_URL %r is not a registered callback URL: callbacks are disabled", CALLBACK_URL)


# -- Callback configuration (lazy, cached on success only) --

_callback_lock = threading.Lock()
_callback_cache: dict = {"config": None, "loaded_at": 0.0}


def _secrets_client():
    """Secrets Manager client. A separate function so offline tests can replace it."""
    client = boto3.client("secretsmanager", region_name=AWS_REGION, config=AWS_CLIENT_CONFIG)
    _log_sdk_retries(client)
    return client


def _read_callback_secret() -> dict:
    """Read and validate the callback credentials. Raises CallbackUnavailable or CallbackError."""
    if not CALLBACK_SECRET_ARN:
        raise CallbackUnavailable("CALLBACK_SECRET_ARN is not set")
    try:
        secret = _secrets_client().get_secret_value(SecretId=CALLBACK_SECRET_ARN)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            # The stack creates the secret empty; it has no value until the notebook writes it.
            raise CallbackUnavailable("callback secret has no value yet") from None
        raise CallbackError(f"Secrets Manager error {code or type(e).__name__}") from None
    except BotoCoreError as e:
        raise CallbackError(f"Secrets Manager request failed: {type(e).__name__}") from None

    try:
        cfg = json.loads(secret.get("SecretString") or "")
    except ValueError:
        raise CallbackUnavailable("callback secret is not valid JSON") from None
    if not isinstance(cfg, dict):
        raise CallbackUnavailable("callback secret is not a JSON object")

    missing = [k for k in _REQUIRED_CALLBACK_FIELDS if not isinstance(cfg.get(k), str) or not cfg.get(k)]
    if missing:
        raise CallbackUnavailable(f"callback secret is missing {', '.join(missing)}")
    if not _COGNITO_TOKEN_RE.fullmatch(cfg["token_endpoint"]):
        raise CallbackUnavailable("token_endpoint is not an Amazon Cognito .../oauth2/token URL in a supported Region")
    if not _SCOPE_RE.fullmatch(cfg["scope"]):
        raise CallbackUnavailable("scope is not one resource-server/scope value")
    return {k: cfg[k] for k in _REQUIRED_CALLBACK_FIELDS}


def _load_callback_config(force: bool = False) -> dict:
    """Return the callback credentials, cached for CALLBACK_CONFIG_TTL seconds after a successful read.

    Failures are never cached: the next call reads Secrets Manager again. `force=True`
    skips the cache (used after a 401 from the customer's API).
    """
    with _callback_lock:
        cached = _callback_cache["config"]
        if not force and cached is not None and time.monotonic() - _callback_cache["loaded_at"] < CALLBACK_CONFIG_TTL:
            return cached
        _callback_cache["config"] = None
        try:
            cfg = _read_callback_secret()
        except CallbackError as e:
            # Never log the secret value; the reason text holds field names only.
            logger.warning("Callback configuration unavailable: %s", e)
            raise
        _callback_cache["config"] = cfg
        _callback_cache["loaded_at"] = time.monotonic()
        logger.info("Loaded callback configuration from Secrets Manager")
        return cfg


# -- OAuth client-credentials token (cache keyed by issuer, client, and scope) --

_token_lock = threading.Lock()
_token_cache: dict[tuple[str, str, str], tuple[str, float]] = {}


def _fetch_token(cfg: dict, force: bool = False) -> str:
    """Return an access token for the callback, cached until shortly before it expires.

    The cache key includes the token endpoint, client ID, and scope, so changed
    credentials never reuse a token issued for the previous settings.
    Raises CallbackError on any failure (fail closed: no unauthenticated callback).
    """
    key = (cfg["token_endpoint"], cfg["client_id"], cfg["scope"])
    with _token_lock:
        cached = _token_cache.get(key)
        if not force and cached and time.monotonic() < cached[1] - _TOKEN_REFRESH_SKEW:
            return cached[0]
        _token_cache.pop(key, None)

    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    try:
        resp = httpx.post(
            cfg["token_endpoint"],
            data={"grant_type": "client_credentials", "scope": cfg["scope"]},
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
            follow_redirects=False,
        )
    except httpx.HTTPError as e:
        # Only the exception type: never the request, which carries the credentials.
        logger.error("Token request to the callback token endpoint failed: %s", type(e).__name__)
        raise CallbackError("token request failed") from None

    request_id = resp.headers.get("x-amz-cognito-request-id", "-")
    if resp.status_code != 200:
        # Log only the status code and request ID; the body can echo sensitive values.
        logger.error("Callback token endpoint returned HTTP %d (request_id=%s)", resp.status_code, request_id)
        raise CallbackError(f"token endpoint returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        body = {}
    access_token = body.get("access_token", "") if isinstance(body, dict) else ""
    if not access_token:
        logger.error("Callback token endpoint returned no access token (request_id=%s)", request_id)
        raise CallbackError("token endpoint returned no access token")

    expires_in = body.get("expires_in", 3600)
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        expires_in = 3600
    with _token_lock:
        _token_cache[key] = (access_token, time.monotonic() + expires_in)
    logger.info("Obtained callback access token (valid %ss, request_id=%s)", expires_in, request_id)
    return access_token


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

# -- Compliance Data (in production: AWS Config, Security Hub, GuardDuty) --
ACCOUNTS = {
    "acc-12345": {"name": "Production Account", "region": "us-east-1", "owner": "platform-team"},
    "acc-67890": {"name": "Staging Account", "region": "us-west-2", "owner": "dev-team"},
    "acc-11111": {"name": "Data Analytics Account", "region": "eu-west-1", "owner": "data-team"},
}

COMPLIANCE_FINDINGS = {
    "acc-12345": [
        {"rule": "S3_BUCKET_PUBLIC_ACCESS", "severity": "CRITICAL", "resource": "arn:aws:s3:::prod-data-lake", "status": "NON_COMPLIANT", "detail": "Public access block not enabled on a bucket that holds confidential data"},
        {"rule": "EC2_INSTANCE_NO_PUBLIC_IP", "severity": "HIGH", "resource": "arn:aws:ec2:us-east-1:111122223333:instance/web-frontend-1", "status": "NON_COMPLIANT", "detail": "EC2 instance in private subnet has public IP assigned"},
        {"rule": "RDS_ENCRYPTION_AT_REST", "severity": "HIGH", "resource": "arn:aws:rds:us-east-1:111122223333:db:prod-orders", "status": "NON_COMPLIANT", "detail": "RDS instance does not have encryption at rest enabled"},
        {"rule": "IAM_MFA_ENABLED", "severity": "MEDIUM", "resource": "arn:aws:iam::111122223333:user/deploy-bot", "status": "NON_COMPLIANT", "detail": "IAM user does not have MFA enabled"},
        {"rule": "CLOUDTRAIL_ENABLED", "severity": "LOW", "resource": "arn:aws:cloudtrail:us-east-1:111122223333:trail/mgmt", "status": "COMPLIANT", "detail": "CloudTrail is enabled with multi-region logging"},
    ],
    "acc-67890": [
        {"rule": "S3_BUCKET_PUBLIC_ACCESS", "severity": "LOW", "resource": "arn:aws:s3:::staging-assets", "status": "COMPLIANT", "detail": "Public access block enabled"},
        {"rule": "IAM_MFA_ENABLED", "severity": "MEDIUM", "resource": "arn:aws:iam::444455556666:user/dev-user", "status": "NON_COMPLIANT", "detail": "IAM user does not have MFA enabled"},
    ],
    "acc-11111": [
        {"rule": "S3_BUCKET_PUBLIC_ACCESS", "severity": "CRITICAL", "resource": "arn:aws:s3:::analytics-raw-data", "status": "NON_COMPLIANT", "detail": "Public access block not enabled on bucket with customer data"},
        {"rule": "REDSHIFT_CLUSTER_ENCRYPTION", "severity": "HIGH", "resource": "arn:aws:redshift:eu-west-1:123456789012:cluster:analytics-prod", "status": "NON_COMPLIANT", "detail": "Redshift cluster does not have encryption enabled"},
    ],
}

REMEDIATIONS = {
    "S3_BUCKET_PUBLIC_ACCESS": {
        "rule": "S3_BUCKET_PUBLIC_ACCESS", "severity": "CRITICAL",
        "steps": ["Navigate to S3 console > Permissions tab", "Enable 'Block all public access'", "Review and remove public bucket policies", "Use IAM Access Analyzer for S3"],
        "aws_cli": "aws s3api put-public-access-block --bucket BUCKET_NAME --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true",
        "estimated_effort": "5 minutes",
    },
    "EC2_INSTANCE_NO_PUBLIC_IP": {
        "rule": "EC2_INSTANCE_NO_PUBLIC_IP", "severity": "HIGH",
        "steps": ["Check if instance needs internet access", "Use NAT Gateway instead of public IP", "Remove Elastic IP", "Restrict security groups"],
        "aws_cli": "aws ec2 modify-network-interface-attribute --network-interface-id ENI_ID --no-associate-public-ip-address (auto-assigned public IP); aws ec2 disassociate-address --association-id ASSOCIATION_ID (Elastic IP)",
        "estimated_effort": "15 minutes",
    },
    "RDS_ENCRYPTION_AT_REST": {
        "rule": "RDS_ENCRYPTION_AT_REST", "severity": "HIGH",
        "steps": ["Create snapshot of unencrypted instance", "Copy snapshot with encryption", "Restore from encrypted snapshot", "Update connection strings", "Delete old instance"],
        "aws_cli": "aws rds copy-db-snapshot --source-db-snapshot-identifier SOURCE --target-db-snapshot-identifier TARGET --kms-key-id KMS_KEY",
        "estimated_effort": "1-2 hours (includes validation)",
    },
    "IAM_MFA_ENABLED": {
        "rule": "IAM_MFA_ENABLED", "severity": "MEDIUM",
        "steps": ["IAM console > Users > Security credentials", "Click 'Assign MFA device'", "Follow setup wizard"],
        "aws_cli": "aws iam enable-mfa-device --user-name USER --serial-number MFA_ARN --authentication-code1 CODE1 --authentication-code2 CODE2",
        "estimated_effort": "5 minutes per user",
    },
}


# -- Compliance Tools --

@tool
def list_accounts() -> list[dict]:
    """List all AWS accounts available for compliance scanning."""
    return [{"account_id": aid, **info} for aid, info in ACCOUNTS.items()]


@tool
def run_compliance_scan(account_id: str) -> dict:
    """Run a compliance scan on a specific AWS account.

    Args:
        account_id: The AWS account ID to scan (e.g., acc-12345)
    """
    if account_id not in ACCOUNTS:
        return {"error": f"Account {account_id} not found."}
    findings = COMPLIANCE_FINDINGS.get(account_id, [])
    non_compliant = [f for f in findings if f["status"] == "NON_COMPLIANT"]
    return {
        "account_id": account_id,
        "account_name": ACCOUNTS[account_id]["name"],
        "scan_summary": {
            "total_rules_checked": len(findings),
            "compliant": len(findings) - len(non_compliant),
            "non_compliant": len(non_compliant),
            "critical_findings": len([f for f in non_compliant if f["severity"] == "CRITICAL"]),
            "high_findings": len([f for f in non_compliant if f["severity"] == "HIGH"]),
        },
        "findings": findings,
    }


@tool
def get_remediation_steps(rule_name: str) -> dict:
    """Get remediation steps for a specific compliance rule violation.

    Args:
        rule_name: The compliance rule name (e.g., S3_BUCKET_PUBLIC_ACCESS)
    """
    return REMEDIATIONS.get(rule_name, {"rule": rule_name, "steps": ["Consult AWS documentation."], "estimated_effort": "Varies"})


# -- A2A callback to the customer's Enterprise Context Agent --

def _post_callback(url: str, payload: dict, token: str) -> httpx.Response:
    """POST one JSON-RPC request with a bearer token. Redirects are never followed."""
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=_CALLBACK_TIMEOUT, follow_redirects=False) as client:
        return client.post(url, json=payload, headers=headers)


def _pinned_base(card_url: str, card_agent_url) -> str | None:
    """Return the base URL a card was fetched from, or None if the card's `url` isn't pinned to it.

    The same rule as the enterprise orchestrator's pinned_agent_url (an offline test
    checks the two agree). The card's `url` must equal the card URL's base (the card
    URL minus /.well-known/agent-card.json). Scheme and host (with port) must match
    exactly (case-insensitive); paths are compared after percent-decoding and with
    trailing slashes ignored. The query must match exactly and neither URL may have
    a fragment.
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


# -- Context Agent Card check (anonymous, before any callback token) --

_context_card_lock = threading.Lock()  # separate from _callback_lock; held only to read or write the entry
_context_card_cache: dict = {"key": None, "verified_at": 0.0}


def _declares_scheme(scheme, cfg: dict) -> bool:
    """True if one securitySchemes entry is OAuth 2.0 client credentials at our token endpoint and scope."""
    if not isinstance(scheme, dict) or scheme.get("type") != "oauth2":
        return False
    flows = scheme.get("flows")
    flow = flows.get("clientCredentials") if isinstance(flows, dict) else None
    if not isinstance(flow, dict):
        return False
    scopes = flow.get("scopes")
    return flow.get("tokenUrl") == cfg["token_endpoint"] and isinstance(scopes, dict) and cfg["scope"] in scopes


def _fetch_and_check_context_card(cfg: dict) -> None:
    """Fetch the customer's context Agent Card anonymously and check it. Raises CallbackUnavailable.

    The GET carries no Authorization header and follows no redirect. The body is
    read in chunks and refused once it passes CARD_MAX_BYTES. The card must be
    pinned to CALLBACK_URL and declare this SaaS provider's token endpoint and
    callback scope (the values in the callback secret).
    """
    card_url = CALLBACK_URL + _WELL_KNOWN_CARD_SUFFIX.lstrip("/")
    request_id = "-"
    try:
        try:
            with httpx.Client(timeout=_CARD_TIMEOUT, follow_redirects=False) as client:
                with client.stream("GET", card_url, headers={"Accept": "application/json"}) as resp:
                    request_id = resp.headers.get("apigw-requestid", "-")
                    if resp.status_code != 200:
                        raise CallbackUnavailable(f"context card HTTP {resp.status_code}")
                    body = bytearray()
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > CARD_MAX_BYTES:
                            raise CallbackUnavailable("context card too large")
            card = json.loads(body)
        except httpx.HTTPError as e:
            raise CallbackUnavailable(f"context card request failed: {type(e).__name__}") from None
        except ValueError as e:
            raise CallbackUnavailable(f"context card is not JSON: {type(e).__name__}") from None

        if not isinstance(card, dict):
            raise CallbackUnavailable("context card is not a JSON object")
        if _pinned_base(card_url, card.get("url")) != CALLBACK_URL:
            raise CallbackUnavailable("context card url does not match the registered callback URL")
        schemes = card.get("securitySchemes")
        if not isinstance(schemes, dict) or not any(_declares_scheme(s, cfg) for s in schemes.values()):
            raise CallbackUnavailable("context card securitySchemes do not name our token endpoint and scope")
    except CallbackUnavailable as e:
        # The reason only: never the card body or any credential.
        logger.warning("Context card check failed: %s (apigw_request_id=%s)", e, request_id)
        raise
    logger.info("Context card verified at %s (apigw_request_id=%s)", card_url, request_id)


def _verify_context_card(cfg: dict, force: bool = False) -> None:
    """Make sure the context card was verified within CONTEXT_CARD_TTL seconds. Raises CallbackUnavailable.

    The cache is keyed by the callback URL, token endpoint, and scope. Any failure
    clears the entry, so a failed check is never cached. The lock is held only to
    read and write the entry, never during the fetch.
    """
    key = (CALLBACK_URL, cfg["token_endpoint"], cfg["scope"])
    with _context_card_lock:
        if (not force and _context_card_cache["key"] == key
                and time.monotonic() - _context_card_cache["verified_at"] < CONTEXT_CARD_TTL):
            return
    try:
        _fetch_and_check_context_card(cfg)  # network and checks, no lock held
    except Exception as e:
        with _context_card_lock:
            _context_card_cache.update(key=None, verified_at=0.0)
        if isinstance(e, CallbackUnavailable):
            raise
        logger.warning("Context card check failed: %s", type(e).__name__)
        raise CallbackUnavailable(f"context card check failed: {type(e).__name__}") from None
    with _context_card_lock:
        _context_card_cache.update(key=key, verified_at=time.monotonic())


def _extract_text(result: dict) -> list[str]:
    """Collect the text parts of an A2A v0.3 Task (artifacts first, then agent history)."""
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
    return texts


def _call_enterprise_context(message_text: str, inbound_task_id: str = "-") -> str:
    """Send a message to the customer's context agent via A2A JSON-RPC and return its text.

    Order: the registered callback URL is valid, the callback credentials load, the
    context card is verified (anonymously), then a token is requested and the message
    is sent to CALLBACK_URL only. Raises CallbackUnavailable or CallbackError on any
    failure, including a JSON-RPC error member in the reply. On a first 401 it reloads
    the credentials, re-verifies the card, forces a new token, and retries once.
    """
    if not CALLBACK_URL_VALID:
        raise CallbackUnavailable("CALLBACK_URL is not a registered callback URL")
    cfg = _load_callback_config()
    _verify_context_card(cfg)
    rpc_id = f"req-{uuid4().hex}"
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

    for attempt in (1, 2):
        token = _fetch_token(cfg, force=attempt > 1)
        try:
            resp = _post_callback(CALLBACK_URL, payload, token)
        except httpx.HTTPError as e:
            logger.error("Callback failed: rpc_id=%s inbound_task_id=%s error=%s", rpc_id, inbound_task_id, type(e).__name__)
            raise CallbackError("callback request failed") from None
        logger.info(
            "Callback response: rpc_id=%s inbound_task_id=%s attempt=%d status=%d apigw_request_id=%s",
            rpc_id, inbound_task_id, attempt, resp.status_code, resp.headers.get("apigw-requestid", "-"),
        )
        if resp.status_code == 401 and attempt == 1:
            # The credentials may have been rotated: reload them, re-verify the card, and retry once.
            logger.info("Callback got 401: reloading the callback configuration and retrying once (rpc_id=%s)", rpc_id)
            cfg = _load_callback_config(force=True)
            _verify_context_card(cfg, force=True)
            continue
        break

    if resp.status_code != 200:
        raise CallbackError(f"callback returned HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise CallbackError("callback reply is not JSON") from None
    if not isinstance(body, dict):
        raise CallbackError("callback reply is not a JSON-RPC object")

    error = body.get("error")
    if error is not None:
        err = error if isinstance(error, dict) else {}
        logger.error(
            "Callback returned a JSON-RPC error: rpc_id=%s code=%s message=%r",
            rpc_id, err.get("code"), str(err.get("message", ""))[:500],
        )
        raise CallbackError("callback returned a JSON-RPC error")

    result = body.get("result")
    if not isinstance(result, dict):
        raise CallbackError("callback reply has no result")
    task_id = result.get("id") if result.get("kind") == "task" else result.get("taskId")
    logger.info(
        "Callback result: rpc_id=%s remote_task_id=%s state=%s",
        rpc_id, task_id or "-", (result.get("status") or {}).get("state", "-"),
    )
    texts = _extract_text(result)
    if not texts:
        raise CallbackError("callback reply has no text")
    # The reply reaches the model as a tool result, which ConverseStream's guardrail doesn't evaluate.
    reply = "\n".join(texts)
    try:
        screen_text(reply, "context_reply", rpc_id)
    except GuardrailBlocked as e:
        logger.warning("Guardrail intervened: stage=context_reply inbound_task_id=%s rpc_id=%s policies=%s",
                       inbound_task_id, rpc_id, e.policies)
        raise CallbackUnavailable("guardrail blocked the context reply") from None
    except GuardrailUnavailable as e:
        raise CallbackError(f"guardrail check failed: {e.reason}") from None
    return reply


@tool(context=True)
def get_enterprise_context_and_contacts(tool_context: ToolContext) -> str:
    """Get the customer's enterprise context in one call: production accounts, compliance requirements, and escalation contacts.

    Call this first, before running any compliance scans.
    """
    request_context = (tool_context.invocation_state or {}).get("a2a_request_context")
    inbound_task_id = getattr(request_context, "task_id", None) or "-"
    logger.info("Calling the customer's Enterprise Context Agent (inbound_task_id=%s)", inbound_task_id)
    try:
        return _call_enterprise_context(
            "List all production accounts. For each one, include: internal policy rules, "
            "data classification, business criticality, and the escalation contacts (team, on-call rotation, channel). "
            "Return everything in one response.",
            inbound_task_id=inbound_task_id,
        )
    except Exception as e:
        # The detail stays in this account's logs; the model (and so the caller) gets one generic message.
        logger.error(
            "Enterprise context callback failed (inbound_task_id=%s): %s: %s", inbound_task_id, type(e).__name__, e,
        )
        return CONTEXT_UNAVAILABLE_MESSAGE


COMPLIANCE_TOOLS = [list_accounts, run_compliance_scan, get_remediation_steps, get_enterprise_context_and_contacts]

SYSTEM_PROMPT = """You are a Compliance SaaS Agent that scans AWS accounts for compliance violations.

IMPORTANT: You MUST follow these steps IN ORDER for every request:

Step 1: Call get_enterprise_context_and_contacts FIRST.
        It returns the customer's production account details, internal policy rules, and escalation contacts.
        If it says enterprise context is unavailable or could not be retrieved, continue with the
        remaining steps and state in your summary that escalation contacts could not be retrieved.

Step 2: Use list_accounts to see all available accounts.

Step 3: Use run_compliance_scan on the relevant accounts.

Step 4: Use get_remediation_steps for any CRITICAL or HIGH findings.

Step 5: Provide a clear summary including:
        - Findings organized by severity (CRITICAL / HIGH / MEDIUM / LOW)
        - The escalation contacts returned in Step 1, or a statement that they could not be retrieved
        - Remediation steps with estimated effort
        - Business impact assessment

NEVER fabricate contact names, email addresses, or chat channels. Only use contacts returned by get_enterprise_context_and_contacts.

Report only what the scan results and the enterprise context show. Don't name laws, regulations, certifications, or industry standards.

Write plain text only: no emoji or other pictographic symbols, such as check marks, warning signs, or colored circles."""

AGENT_NAME = "Compliance SaaS Agent"
AGENT_DESCRIPTION = (
    "Scans AWS accounts for compliance with your organization's policies and returns findings by severity with "
    "remediation steps."
)




class _GuardrailMonitor(HookProvider):
    """Logs a guardrail intervention on a model call, with the policies from the stream's trace.

    One per agent (one agent per A2A context). Neither entry point can raise, so it never ends the stream or
    keeps the AfterModelCallEvent callbacks that run after it (Strands' retry strategy) from running.
    """

    def __init__(self):
        self._trace = None

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self._before_model_call)
        registry.add_callback(AfterModelCallEvent, self._after_model_call)

    def __call__(self, **kwargs) -> None:
        """The Strands callback handler: keeps the guardrail trace from the stream's metadata event."""
        try:
            trace = (((kwargs.get("event") or {}).get("metadata") or {}).get("trace") or {}).get("guardrail")
            if trace is not None:
                self._trace = trace
        except Exception:  # never break the stream
            return

    def _before_model_call(self, event: BeforeModelCallEvent) -> None:
        self._trace = None

    def _after_model_call(self, event: AfterModelCallEvent) -> None:
        try:
            resp = event.stop_response  # None when the model call raised
            if resp is None or resp.stop_reason != "guardrail_intervened":
                return
            ctx = (event.invocation_state or {}).get("a2a_request_context")
            logger.warning("Guardrail intervened: task_id=%s stage=model policies=%s",
                           getattr(ctx, "task_id", "-"), policy_summary(self._trace))
        except Exception:  # never break the event loop or the retry strategy
            logger.debug("Guardrail monitor could not summarize the trace")


class _RequireGuardrail(HookProvider):
    """Without a configured guardrail, every model call fails before it is sent (fail closed)."""

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeModelCallEvent, self._refuse)

    def _refuse(self, event: BeforeModelCallEvent) -> None:
        raise GuardrailUnavailable("model", "not configured")


def create_compliance_agent(context_id: str) -> Agent:
    """Build a fresh agent for one A2A context (used as the A2AServer agent_factory)."""
    monitor = _GuardrailMonitor()
    # Strands also builds one agent at startup for the Agent Card and never calls its model, so the factory itself
    # doesn't fail: without a configured guardrail, the card is served and every model call is refused.
    hooks = [monitor] if GUARDRAIL_CONFIGURED else [monitor, _RequireGuardrail()]
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        boto_client_config=BEDROCK_CLIENT_CONFIG,
        guardrail_id=GUARDRAIL_ID,
        guardrail_version=GUARDRAIL_VERSION,
        guardrail_trace="enabled",
        guardrail_stream_processing_mode="sync",
        # The orchestrator's message, the latest user text, goes in guardContent on every call of the tool loop.
        guardrail_latest_message=True,
    )
    _log_sdk_retries(model.client)
    # One retry layer: the SDK retries a throttle before the stream starts. Strands' own retry stays off,
    # because a retry after partial output would repeat text already streamed into the A2A reply.
    return Agent(
        model=model,
        name=AGENT_NAME,
        description=AGENT_DESCRIPTION,
        tools=COMPLIANCE_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        callback_handler=monitor,
        hooks=hooks,
        retry_strategy=None,
    )


# The one skill published on the Agent Card. Callers' models read card text as tool
# descriptions, so it describes the product, not this agent's internal tool names.
COMPLIANCE_SCAN_SKILL = AgentSkill(
    id="compliance-scan",
    name="Compliance scan",
    description=(
        "Scans your AWS accounts against your organization's policies and returns findings by severity, "
        "remediation steps with estimated effort, and your escalation contacts when available."
    ),
    tags=["compliance", "security", "aws"],
    examples=["Run a compliance scan on our production accounts and summarize the critical findings."],
)


# -- A2A server --

def _card_security() -> tuple[dict[str, SecurityScheme], list[dict[str, list[str]]]]:
    """Build the card's securitySchemes and security. Fails closed on missing or invalid settings."""
    if not _is_https_url(CARD_AUTH_URL, "/oauth2/token"):
        raise RuntimeError("CARD_AUTH_URL must be the https .../oauth2/token endpoint callers use")
    if not CARD_AUTH_SCOPE or "/" not in CARD_AUTH_SCOPE or any(c.isspace() for c in CARD_AUTH_SCOPE):
        raise RuntimeError("CARD_AUTH_SCOPE must be one resource-server/scope value")
    scheme = OAuth2SecurityScheme(
        description="OAuth 2.0 client credentials from the customer's identity provider, registered at onboarding.",
        flows=OAuthFlows(
            client_credentials=ClientCredentialsOAuthFlow(
                token_url=CARD_AUTH_URL,
                scopes={CARD_AUTH_SCOPE: "Invoke the compliance agent"},
            )
        ),
    )
    return {"cognito": SecurityScheme(root=scheme)}, [{"cognito": [CARD_AUTH_SCOPE]}]


class _TaskLoggingExecutor(AgentExecutor):
    """Logs the task, context, and request IDs of every inbound A2A call, then delegates."""

    def __init__(self, inner: AgentExecutor):
        self._inner = inner

    @staticmethod
    def _ids(context: RequestContext) -> tuple[str, str]:
        state = context.call_context.state if context.call_context else {}
        headers = state.get("headers") or {}
        return (
            headers.get("x-amzn-requestid", "-"),
            headers.get("x-amzn-bedrock-agentcore-runtime-session-id", "-"),
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        request_id, session_id = self._ids(context)
        logger.info(
            "Inbound A2A request: task_id=%s context_id=%s request_id=%s session_id=%s",
            context.task_id, context.context_id, request_id, session_id,
        )
        # Only text reaches the model: a file or data part would become a block the guardrail doesn't evaluate.
        parts = getattr(getattr(context, "message", None), "parts", None) or []
        kinds = sorted({getattr(getattr(p, "root", p), "kind", "unknown") for p in parts} - {"text"})
        if kinds:
            logger.warning("Rejected inbound A2A message: non-text parts kinds=%s task_id=%s request_id=%s",
                           kinds, context.task_id, request_id)
            raise ServerError(error=ContentTypeNotSupportedError(message="Only text parts are accepted."))
        await self._inner.execute(context, event_queue)
        logger.info("Inbound A2A request finished: task_id=%s", context.task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        logger.info("Inbound A2A cancel: task_id=%s", context.task_id)
        await self._inner.cancel(context, event_queue)


class SecuredA2AServer(A2AServer):
    """A2AServer whose card declares how to authenticate and whose inbound calls are logged."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._security_schemes, self._security = _card_security()
        self.request_handler.agent_executor = _TaskLoggingExecutor(self.request_handler.agent_executor)

    @property
    def public_agent_card(self) -> AgentCard:
        return super().public_agent_card.model_copy(
            update={"security_schemes": self._security_schemes, "security": self._security}
        )


_NO_DOCS = {"docs_url": None, "redoc_url": None, "openapi_url": None}

# No push-notification store or sender: a push-notification URL would be a caller-chosen destination.
a2a_server = SecuredA2AServer(
    agent_factory=create_compliance_agent,
    http_url=AGENT_URL,
    serve_at_root=True,
    skills=[COMPLIANCE_SCAN_SKILL],
    enable_a2a_compliant_streaming=True,
)

app = FastAPI(title="Compliance SaaS Agent (A2A)", **_NO_DOCS)


@app.get("/ping")
def ping():
    # AgentCore runtime health-check contract.
    return {"status": "Healthy"}


app.mount("/", a2a_server.to_fastapi_app(app_kwargs=_NO_DOCS))

if __name__ == "__main__":
    logger.info("Starting Compliance SaaS Agent on %s:%d", BIND_HOST, PORT)
    uvicorn.run(app, host=BIND_HOST, port=PORT)
