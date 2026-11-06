# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Enterprise Context Agent - LangChain + A2A (a2a-sdk)

The enterprise's internal agent that provides account metadata, compliance
requirements, and escalation contacts. The Compliance SaaS Agent calls this
via A2A for bidirectional collaboration.

Trust model: the SaaS agent gets its token from the SaaS provider's own
identity provider. The partner API's JWT authorizer trusts only that issuer and
accepts only the SaaS callback client registered at onboarding, with the
callback scope. No credential is shared between accounts. The Agent Card stays
public, so the SaaS agent can fetch and check it anonymously before it requests
a callback token.

This agent is built with LangChain (same framework choice as the orchestrator)
to show that the enterprise standardized on LangChain internally. The ISV's
Strands-based agent communicates with it via A2A without knowing or caring
about the framework choice.

Key design:
- LangChain + ChatBedrock (Nova Lite) for reasoning
- Tools are plain Python functions decorated with @langchain_tool
- A2A endpoint served by the official a2a-sdk (handles the protocol for you)
- Agent Card published at /.well-known/agent-card.json

A2A is JSON-RPC over HTTP, so this endpoint could also be hand-rolled
(serve the Agent Card and handle message/send yourself). This agent uses the
official a2a-sdk instead; the manual approach is a valid alternative.

The SDK server runs in v0.3 backward-compatibility mode so it keeps accepting
JSON-RPC `message/send` and replies in the v0.3 shape
(`result.artifacts[].parts[]` with text parts) that the Strands SaaS agent and
the orchestrator already speak - no change to those callers is required. The
Agent Card lists a v0.3 interface next to the v1.0 one, so v0.3 clients find the
top-level `url`, `protocolVersion`, and `preferredTransport` fields they expect.

Environment (all non-secret, set by the ECS task definition):
- CONTEXT_AGENT_URL: the public URL callers use (the partner API Gateway URL)
- CARD_AUTH_URL, CARD_AUTH_SCOPE: the SaaS provider's OAuth2 token endpoint
  and the callback scope, declared in the Agent Card's securitySchemes. The API
  Gateway JWT authorizer enforces them; the card itself stays public and static.
- BIND_HOST: listen address, loopback (127.0.0.1) by default; the container
  image sets it to all interfaces for container networking
- GUARDRAIL_ID, GUARDRAIL_VERSION: this account's Amazon Bedrock guardrail (its ID, never an ARN) and a
  numbered version; without both, the agent makes no model call

Deploy: ECS Fargate (behind NLB + API Gateway with JWT auth)
"""

import asyncio
import json
import logging
import os
import re
from urllib.parse import urlparse

import uvicorn
from botocore.config import Config
from langchain.agents import create_agent
from langchain_aws import ChatBedrock
from langchain_core.tools import tool as langchain_tool
from starlette.applications import Starlette

from a2a.helpers import new_task, new_text_part
from a2a.server.agent_execution import AgentExecutor as A2AAgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    ClientCredentialsOAuthFlow,
    OAuth2SecurityScheme,
    OAuthFlows,
    SecurityRequirement,
    SecurityScheme,
    StringList,
    TaskState,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("enterprise-context-agent")

# Small AWS calls: the SDK's in-Region connect default, a short read, standard retries, 3 attempts in all.
AWS_CLIENT_CONFIG = Config(connect_timeout=1.1, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 3})
# Converse with Nova Lite inside the partner API's 30-second limit: 2 attempts of at most 11.1 seconds each,
# plus at most 1 second of backoff, end within 23.2 seconds.
BEDROCK_CLIENT_CONFIG = Config(connect_timeout=1.1, read_timeout=10, retries={"mode": "standard", "total_max_attempts": 2})


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


# -- Configuration --
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
CONTEXT_AGENT_URL = os.environ.get("CONTEXT_AGENT_URL", "http://127.0.0.1:9001/")

# Declared in the Agent Card so callers know how to authenticate. The partner
# API Gateway JWT authorizer enforces them before a request reaches this agent.
CARD_AUTH_URL = os.environ.get("CARD_AUTH_URL", "")
CARD_AUTH_SCOPE = os.environ.get("CARD_AUTH_SCOPE", "")

# Loopback by default. The container image sets BIND_HOST to all interfaces so
# the NLB can reach the task; the task's security group admits only the NLB.
BIND_HOST = os.environ.get("BIND_HOST", "127.0.0.1")
PORT = 9001

# Returned to the remote caller when the agent fails; the detail goes to the logs only.
GENERIC_ERROR_MESSAGE = "The context agent could not process the request."
# Returned when this account's guardrail blocks the request or the answer.
GUARDRAIL_BLOCKED_MESSAGE = ("Enterprise context unavailable: the enterprise's Amazon Bedrock guardrail blocked "
                             "this request.")

# -- Enterprise Account Metadata --
# In production: a configuration management database or an internal API

ACCOUNT_METADATA = {
    "acc-12345": {
        "account_id": "acc-12345",
        "name": "Production Account",
        "environment": "production",
        "business_criticality": "critical",
        "data_classification": "confidential",
        "policy_rules": [
            "POL-STORAGE-01: No public storage in production",
            "POL-ENCRYPT-02: Encrypt confidential data at rest",
            "POL-NET-03: No public IP addresses on production instances",
            "POL-IAM-04: MFA for IAM users",
        ],
        "owner": {
            "team": "Platform Engineering",
            "on_call": "Platform on-call",
            "rotation": "PLAT-ONCALL-7",
            "channel": "#platform-oncall",
        },
        "notes": ("Customer-facing order processing; business critical; holds confidential data. "
                  "For critical findings, also page the security response rotation SECRESP-ONCALL-1 within 24 hours."),
    },
    "acc-67890": {
        "account_id": "acc-67890",
        "name": "Staging Account",
        "environment": "staging",
        "business_criticality": "medium",
        "data_classification": "internal",
        "policy_rules": ["POL-IAM-04: MFA for IAM users"],
        "owner": {
            "team": "Application Development",
            "on_call": "Application on-call",
            "rotation": "APP-ONCALL-2",
            "channel": "#app-oncall",
        },
        "notes": "Staging environment. No real customer data.",
    },
    "acc-11111": {
        "account_id": "acc-11111",
        "name": "Data Analytics Account",
        "environment": "production",
        "business_criticality": "high",
        "data_classification": "confidential",
        "policy_rules": [
            "POL-STORAGE-01: No public storage in production",
            "POL-ENCRYPT-02: Encrypt confidential data at rest",
        ],
        "owner": {
            "team": "Data Engineering",
            "on_call": "Data platform on-call",
            "rotation": "DATA-ONCALL-3",
            "channel": "#data-oncall",
        },
        "notes": "Analytics data lake; holds confidential data; quarterly policy review.",
    },
}


# -- LangChain Tools --

@langchain_tool
def get_account_metadata(account_id: str) -> str:
    """Get detailed metadata for a specific enterprise account including environment,
    criticality, internal policy rules, data classification, and owner contacts.

    Args:
        account_id: The account ID (e.g., acc-12345)
    """
    result = ACCOUNT_METADATA.get(account_id, {"error": f"Account {account_id} not found."})
    return json.dumps(result, indent=2)


@langchain_tool
def list_production_accounts() -> str:
    """List all enterprise accounts classified as production.
    Returns accounts with their internal policy rules, criticality, data classification,
    and escalation contacts: the owner (team, on_call, rotation, channel) and the
    escalation_note. This one call includes the escalation contacts, so no other tool call
    is needed for them.
    """
    # Same owner and escalation_note keys and values as get_escalation_contacts, so one
    # call answers a request for every production account and its contacts.
    accounts = [
        {
            "account_id": m["account_id"],
            "name": m["name"],
            "business_criticality": m["business_criticality"],
            "policy_rules": m["policy_rules"],
            "data_classification": m["data_classification"],
            "owner": m["owner"],
            "escalation_note": m["notes"],
        }
        for m in ACCOUNT_METADATA.values()
        if m["environment"] == "production"
    ]
    return json.dumps(accounts, indent=2)


@langchain_tool
def get_compliance_requirements(account_id: str) -> str:
    """Get the internal policy rules that apply to an account.

    Args:
        account_id: The account ID (e.g., acc-12345)
    """
    if account_id not in ACCOUNT_METADATA:
        return json.dumps({"error": f"Account {account_id} not found."})
    m = ACCOUNT_METADATA[account_id]
    return json.dumps({
        "account_id": account_id,
        "name": m["name"],
        "policy_rules": m["policy_rules"],
        "data_classification": m["data_classification"],
        "notes": m["notes"],
    }, indent=2)


@langchain_tool
def get_escalation_contacts(account_id: str) -> str:
    """Get escalation contacts for compliance issues on an account.

    Args:
        account_id: The account ID (e.g., acc-12345)
    """
    if account_id not in ACCOUNT_METADATA:
        return json.dumps({"error": f"Account {account_id} not found."})
    m = ACCOUNT_METADATA[account_id]
    return json.dumps({
        "account_id": account_id,
        "name": m["name"],
        "business_criticality": m["business_criticality"],
        "owner": m["owner"],
        "escalation_note": m["notes"],
    }, indent=2)


# -- LangChain Agent --

context_tools = [get_account_metadata, list_production_accounts, get_compliance_requirements, get_escalation_contacts]

llm = ChatBedrock(
    model_id="us.amazon.nova-lite-v1:0",
    region_name=AWS_REGION,
    config=BEDROCK_CLIENT_CONFIG,
    guardrails=GUARDRAIL_CONFIG,  # Converse evaluates the whole request: the SaaS agent's message is the user turn
)
_log_sdk_retries(llm.client)  # the Converse path reuses this client

SYSTEM_PROMPT = """You are the enterprise's internal context agent. You provide accurate
metadata about the enterprise's AWS accounts to authorized external agents.

Use only what the tools return:
- Answer only with values the tools returned. Never invent or guess names, email addresses,
  chat channels, on-call rotations, account IDs, or policy rules.
- If a value was not returned by a tool, say that it is not available.
- For questions about production accounts, call list_production_accounts first. It includes
  the escalation contacts for each account (owner team, on-call rotation, chat channel) and the
  escalation note.

When asked about accounts:
- Return the specific metadata requested
- Include escalation contacts when they are requested or findings are critical
- Flag accounts that hold confidential data as requiring extra attention

Report only what the scan results and the enterprise context show. Don't name laws, regulations,
certifications, or industry standards.

Be plain, concise, and factual. Return structured data."""

# create_agent builds the tool-calling loop: the model sees the system prompt
# and the account tools above, calls a tool when it needs one, and gets the
# result back until it can give a final answer.
agent_executor = create_agent(llm, context_tools, system_prompt=SYSTEM_PROMPT)

# Allow at most 5 tool-calling rounds. Each round is a model step plus a tool
# step, and the final answer is one more step: recursion_limit = 2 * 5 + 1.
AGENT_RECURSION_LIMIT = 2 * 5 + 1


# -- A2A Endpoint (official a2a-sdk) --
# The a2a-sdk implements the A2A protocol (Agent Card + JSON-RPC) for us.
# A2A is JSON-RPC over HTTP, so this could be hand-rolled instead. We run the
# SDK in v0.3 compatibility mode so the wire format stays identical to what the
# existing callers expect.

def _card_security() -> tuple[dict[str, SecurityScheme], list[SecurityRequirement]]:
    """Build the card's securitySchemes and security requirements. Fails closed on invalid settings."""
    parsed = urlparse(CARD_AUTH_URL)
    if parsed.scheme != "https" or not parsed.netloc or not parsed.path.endswith("/oauth2/token"):
        raise RuntimeError("CARD_AUTH_URL must be the https .../oauth2/token endpoint callers use")
    if not CARD_AUTH_SCOPE or "/" not in CARD_AUTH_SCOPE or any(c.isspace() for c in CARD_AUTH_SCOPE):
        raise RuntimeError("CARD_AUTH_SCOPE must be one resource-server/scope value")
    scheme = SecurityScheme(
        oauth2_security_scheme=OAuth2SecurityScheme(
            description=(
                "OAuth 2.0 client credentials from the SaaS provider's identity provider; "
                "only the client registered at onboarding is accepted."
            ),
            flows=OAuthFlows(
                client_credentials=ClientCredentialsOAuthFlow(
                    token_url=CARD_AUTH_URL,
                    scopes={CARD_AUTH_SCOPE: "Call the enterprise context agent"},
                )
            ),
        )
    )
    requirement = SecurityRequirement(schemes={"cognito": StringList(list=[CARD_AUTH_SCOPE])})
    return {"cognito": scheme}, [requirement]


_SECURITY_SCHEMES, _SECURITY_REQUIREMENTS = _card_security()

# The card is static: it's built once at startup and served as-is, with no model call.
AGENT_CARD = AgentCard(
    name="Enterprise Context Agent",
    description=(
        "Provides enterprise account metadata including environment classification, "
        "internal policy rules, data classification, business criticality, "
        "team ownership, and escalation contacts."
    ),
    version="1.0.0",
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False),
    supported_interfaces=[
        AgentInterface(
            url=CONTEXT_AGENT_URL,
            protocol_binding="JSONRPC",
            protocol_version="1.0",
        ),
        # The wire protocol both callers use. Listing it makes the served card also
        # carry the v0.3 top-level fields (url, protocolVersion, preferredTransport).
        AgentInterface(
            url=CONTEXT_AGENT_URL,
            protocol_binding="JSONRPC",
            protocol_version="0.3",
        ),
    ],
    security_schemes=_SECURITY_SCHEMES,
    security_requirements=_SECURITY_REQUIREMENTS,
    skills=[
        AgentSkill(
            id="account-metadata",
            name="account-metadata",
            description=(
                "Get detailed metadata for enterprise AWS accounts including environment, "
                "criticality, internal policy rules, and owner contacts."
            ),
            tags=["enterprise", "compliance", "metadata"],
            examples=[
                "List all production accounts with their compliance requirements",
                "Get escalation contacts for account acc-12345",
                "Which internal policies apply to acc-12345?",
            ],
        ),
    ],
)


def _run_langchain_agent(query: str) -> str:
    """Invoke the LangChain agent and return its final text response.

    Mirrors the previous manual handler: flattens Bedrock structured content
    blocks into a string and strips any <thinking> tags some models emit.
    """
    if not GUARDRAIL_CONFIGURED:
        raise GuardrailUnavailable("model", "not configured")
    result = agent_executor.invoke(
        {"messages": [{"role": "user", "content": query}]},
        {"recursion_limit": AGENT_RECURSION_LIMIT},
    )
    metadata = getattr(result["messages"][-1], "response_metadata", None) or {}
    if metadata.get("stopReason") == "guardrail_intervened":
        raise GuardrailBlocked("model", policy_summary(metadata.get("trace")))
    response_text = result["messages"][-1].content
    # LangChain may return a list of content blocks instead of a string
    # (happens with models that return structured content).
    if isinstance(response_text, list):
        response_text = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in response_text
        )
    response_text = str(response_text)
    # Strip <thinking> tags that some models include.
    response_text = re.sub(r"<thinking>.*?</thinking>\s*", "", response_text, flags=re.DOTALL)
    return response_text


class ContextAgentExecutor(A2AAgentExecutor):
    """Bridges the A2A request/response lifecycle to the LangChain agent.

    Returns a completed Task carrying a single text artifact, which the SDK
    serializes (in v0.3 compat mode) as result.artifacts[].parts[] with a text
    part - exactly what the Strands SaaS agent and orchestrator parse.
    """

    @staticmethod
    def _ids(context: RequestContext) -> tuple[str, str]:
        """JSON-RPC request ID and trace header of the inbound call, for cross-account correlation."""
        state = context.call_context.state if context.call_context else {}
        headers = state.get("headers") or {}
        return str(state.get("request_id", "-")), headers.get("x-amzn-trace-id", "-")

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = context.get_user_input() or ""
        rpc_id, trace_id = self._ids(context)
        logger.info(
            "A2A request received: task_id=%s context_id=%s rpc_id=%s trace_id=%s query=%r",
            context.task_id, context.context_id, rpc_id, trace_id, query[:100],
        )

        task = context.current_task
        if task is None:
            task = new_task(context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()

        # LangChain's invoke is blocking - run it off the event loop.
        try:
            response_text = await asyncio.to_thread(_run_langchain_agent, query)
        except GuardrailBlocked as e:
            logger.warning("Guardrail intervened: task_id=%s rpc_id=%s policies=%s", context.task_id, rpc_id, e.policies)
            response_text = GUARDRAIL_BLOCKED_MESSAGE
        except Exception:
            # The detail stays in this account's logs; the remote caller gets a generic message.
            logger.exception("Agent execution failed: task_id=%s rpc_id=%s", context.task_id, rpc_id)
            response_text = GENERIC_ERROR_MESSAGE

        logger.info("A2A response: task_id=%s rpc_id=%s chars=%d", context.task_id, rpc_id, len(response_text))
        await updater.add_artifact([new_text_part(response_text)], name="response")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Context agent does not support cancel")


# No push-notification store or sender: a push-notification URL would be a caller-chosen destination.
request_handler = DefaultRequestHandler(
    agent_executor=ContextAgentExecutor(),
    task_store=InMemoryTaskStore(),
    agent_card=AGENT_CARD,
)

routes = create_agent_card_routes(AGENT_CARD)  # GET /.well-known/agent-card.json
routes += create_jsonrpc_routes(
    request_handler,
    "/",  # POST /  (accepts message/send in v0.3 compatibility mode)
    enable_v0_3_compat=True,
)

app = Starlette(routes=routes)


if __name__ == "__main__":
    logger.info("Starting Enterprise Context Agent (LangChain + a2a-sdk) on %s:%d", BIND_HOST, PORT)
    logger.info("Agent Card at: /.well-known/agent-card.json (public, static)")
    logger.info("Tools: %s", [t.name for t in context_tools])
    uvicorn.run(app, host=BIND_HOST, port=PORT)
