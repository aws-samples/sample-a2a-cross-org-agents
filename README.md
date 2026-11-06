# Build composable, interoperable multi-agent systems with A2A on AWS

> **Disclaimer:** This is sample code for demonstration and educational purposes only. It is not intended for production use without additional security review, testing, and hardening.

Cross-organization agent collaboration over the [Agent-to-Agent (A2A) protocol](https://github.com/a2aproject/A2A), in two AWS accounts: an **enterprise account** and a **SaaS provider account**. Each organization runs its own identity provider (an Amazon Cognito user pool) that issues tokens only to its own workloads. Each side trusts the other side's identity provider for one client and one scope. No client secret, key, or AWS Identity and Access Management (IAM) permission is shared between the accounts.

This sample shows:

- **Agent Card discovery**: the enterprise learns what the SaaS agent can do from its card, with no hardcoded routing.
- **Interoperability**: a LangChain orchestrator on Amazon Elastic Container Service (Amazon ECS) talks to a Strands agent on Amazon Bedrock AgentCore over A2A, without either knowing the other's framework.
- **Composability**: the SaaS compliance agent calls back to the enterprise's context agent for metadata it does not have, so the answer combines both organizations' data.
- **Two-way workload identity federation**: each side's identity provider issues tokens to its own workloads, and the other side trusts that provider for one client and scope.
- **Onboarding with public values only**: the enterprise sends an onboarding request and the SaaS provider sends back a response. Neither contains a secret, and nobody types or copies a secret.

## Architecture

![Architecture diagram. Left, the enterprise account: the enterprise app, the enterprise user pool (Amazon Cognito), the user API and the partner API (Amazon API Gateway), the orchestrator and the context agent on Amazon ECS on AWS Fargate, AWS Secrets Manager, and Amazon Bedrock. Right, the SaaS provider account: the compliance agent on Amazon Bedrock AgentCore Runtime, the SaaS user pool (Amazon Cognito), AWS Secrets Manager, and Amazon Bedrock. Numbered arrows 0 to 7 show the trust registration between the accounts and one request: the app's token and HTTPS call to the user API, the orchestrator's token and A2A call to the compliance agent, and the compliance agent's token and A2A callback to the context agent.](architecture-diagram.png)

The numbers in the diagram:

0. Trust registration, once, at onboarding. The two accounts exchange public values only: the OIDC issuer, client IDs, scopes, and URLs, in `onboarding-request.json` and `onboarding-response.json`. With them, the SaaS provider's AgentCore runtime trusts the enterprise pool, and the enterprise partner API trusts the SaaS pool.
1. The enterprise app (notebook 3 in the demo) gets a token from the enterprise user pool, as client `a2a-enterprise-orchestrator-user` with scope `a2a-enterprise-orchestrator/ask`.
2. It calls the user API over HTTPS: `POST /jobs` with the question, then `GET /jobs/{job_id}` until the job is done. The `user-jwt` authorizer checks the token.
3. The user API passes the request through the VPC link and the internal NLB (port 8000) to the orchestrator on Amazon ECS on AWS Fargate.
4. The orchestrator gets a token from the enterprise user pool, as client `a2a-enterprise-orchestrator-saas` with scope `a2a-enterprise-compliance-saas/invoke`. That client's secret is in the enterprise account's Secrets Manager (`a2a-enterprise/orchestrator-credentials`).
5. With that token, the orchestrator reads the compliance agent's Agent Card at startup. When its model picks the agent's tool, it sends A2A `message/send` over HTTPS to the compliance agent on Amazon Bedrock AgentCore Runtime, whose JWT authorizer accepts only this client and scope from the enterprise pool.
6. When the compliance agent needs enterprise context, it checks the context agent's public Agent Card at the callback URL registered at onboarding, and only then gets a token from the SaaS user pool, as client `a2a-saas-callback-client` with scope `a2a-saas-customer-callback/invoke`. That client's secret is in the SaaS account's Secrets Manager (`a2a-saas/callback-credentials`).
7. It sends the A2A callback with that token over HTTPS to the enterprise partner API (`POST /`), whose `saas-jwt` authorizer accepts only the callback client and scope from the SaaS pool. The partner API passes it through the VPC link and the internal NLB (port 9001) to the context agent. The replies go back the same way, and the app reads the orchestrator's answer at its next `GET /jobs/{job_id}`.

Each agent calls Amazon Bedrock in its own account, with that account's guardrail on every call (the dashed "invoke model" lines): the orchestrator and the compliance agent use Claude Sonnet 4.6, and the context agent uses Amazon Nova Lite. The compliance agent's tools, including the one that sends the callback, are Strands `@tool` functions in its own process. [Security controls](#security-controls) describes the controls on each hop.

The same architecture as text:

```
Enterprise account                                       SaaS provider account
 Identity provider: Cognito a2a-enterprise-pool           Identity provider: Cognito a2a-saas-pool
   a2a-enterprise-orchestrator-saas  (secret stays here)    a2a-saas-callback-client (secret stays here)
   a2a-enterprise-orchestrator-user  (secret stays here)

 caller --JWT (enterprise pool, orchestrator-user)--> API Gateway user API
   --VPC link--> internal NLB :8000 --> Orchestrator (ECS, LangChain)
        | token from the ENTERPRISE pool (orchestrator-saas, scope a2a-enterprise-compliance-saas/invoke)
        +--HTTPS + JWT--> AgentCore runtime: trusts the enterprise pool only
                          (discoveryUrl, allowedClients, allowedScopes)
                          Compliance agent (Strands)
                            | token from the SAAS pool (callback client, scope a2a-saas-customer-callback/invoke)
                            | sent only to the callback URL registered at onboarding
 Context agent (ECS) <-- NLB :9001 <--VPC link-- API Gateway partner API <--HTTPS + JWT--+
   partner API trusts the SaaS pool only (issuer, callback client ID, route scope)

 What crosses accounts: two files of public values (onboarding-request.json, onboarding-response.json)
 and HTTPS calls with short-lived tokens. No secret, key, or IAM permission crosses.
```

| Call | Caller | Token issued by | Checked by |
|---|---|---|---|
| Agent Card fetch and A2A `message/send` to the compliance agent | Orchestrator (enterprise) | Enterprise pool, client `a2a-enterprise-orchestrator-saas`, scope `a2a-enterprise-compliance-saas/invoke` | Amazon Bedrock AgentCore JWT authorizer: `discoveryUrl` (the enterprise pool), `allowedClients`, `allowedScopes` |
| A2A callback `message/send` to the context agent | Compliance agent (SaaS provider) | SaaS pool, client `a2a-saas-callback-client`, scope `a2a-saas-customer-callback/invoke` | Partner API JWT authorizer `saas-jwt`: SaaS issuer, callback client ID, route scope |
| Context agent's Agent Card | Anyone (the compliance agent checks it before it requests a callback token) | none | Public by design: a static discovery document with no model call |
| `POST /jobs`, `GET /jobs/{job_id}`, `GET /health`, `GET /agents` | Enterprise app (in the demo, notebook 3) | Enterprise pool, client `a2a-enterprise-orchestrator-user`, scope `a2a-enterprise-orchestrator/ask` | User API JWT authorizer `user-jwt`: issuer, client ID, route scope |

**What crosses accounts.** Two files of public values, written once at onboarding, and HTTPS calls carrying 15-minute access tokens. Each token is sent to the other account, but it was issued by the caller's own pool, so the receiving side only needs the issuer's public signing keys to check it.

### Trust model

Each organization's pool issues tokens only to its own workloads, and every client secret stays in the account that created it. The other side's gate trusts that pool by its public issuer URL, one client ID, and one scope:

- The SaaS provider's AgentCore runtime trusts the enterprise pool (`discoveryUrl`), accepts only the `a2a-enterprise-orchestrator-saas` client (`allowedClients`), and requires `a2a-enterprise-compliance-saas/invoke` (`allowedScopes`).
- The enterprise partner API trusts the SaaS pool (`Issuer`), accepts only the `a2a-saas-callback-client` client (`Audience`, checked against `client_id`), and requires `a2a-saas-customer-callback/invoke` on `POST /`.

This works like an IAM role that trusts an external OIDC identity provider: the role never holds a secret from that provider, it checks that the token comes from the provider's issuer and names the expected subject. Here, each gate checks that the token comes from the partner's issuer and names the one client and scope registered at onboarding. See [Create an OpenID Connect (OIDC) identity provider in IAM](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc.html).

### Agent Card discovery flow

1. The SaaS provider launches this customer's AgentCore runtime. Its A2A server publishes an Agent Card at `/.well-known/agent-card.json` with one curated skill and a `securitySchemes` entry that names the customer's own token endpoint and the `a2a-enterprise-compliance-saas/invoke` scope.
2. At startup the orchestrator checks that the card URL is an AgentCore runtime card URL, gets a token from the enterprise pool (step 4 in the diagram), and fetches the card with it.
3. The orchestrator keeps the card only if its `url` matches the address it was fetched from (card pinning) and its `securitySchemes` name the enterprise's own token endpoint and scope. Its token goes only to that pinned URL.
4. Each skill on the card becomes a LangChain tool, and the skill description is what the model reads.
5. A caller submits a question (steps 1 to 3). The model picks the tool, and the tool sends an A2A JSON-RPC `message/send` to the pinned URL (step 5).
6. When the compliance agent needs enterprise context, it first fetches the context agent's card anonymously from the callback URL registered at onboarding. The card's `url` must equal that URL, and its `securitySchemes` must name the SaaS token endpoint and the callback scope. Only then does the agent get a token from the SaaS pool (step 6) and send the callback (step 7). A verified card is reused for 5 minutes; a failed check is never cached, and the agent then continues without enterprise context.

### Key concepts

| Concept | Description |
|---|---|
| A2A protocol | An open standard for agent-to-agent communication: JSON-RPC 2.0 over HTTPS with a defined message format (`message/send`). Both directions in this demo speak A2A v0.3. The context agent runs a2a-sdk 1.2.2 in v0.3 compatibility mode; the compliance agent runs Strands with a2a-sdk 0.3. |
| Agent Card | A JSON document at `/.well-known/agent-card.json` that describes an agent: its endpoint `url`, its skills, and, through `securitySchemes`, how a caller gets a token (OAuth 2.0 client credentials, token URL, and scope). |
| Workload identity federation | Each side's identity provider issues tokens to its own workloads. The other side's gate trusts that provider's issuer, one client ID, and one scope, so no secret is shared. |
| Card pinning | The orchestrator sends its token only to the address the card was fetched from: it keeps a card only if the card's `url` equals the card URL minus `/.well-known/agent-card.json`. |
| Registered callback | The compliance agent calls back only to the URL registered at onboarding, and only after it checks the public context card there. No caller can name another destination. |
| Curated skills | The compliance agent publishes one curated skill on its card instead of its internal tool names. Callers' models read card text as tool descriptions, so the card says only what a customer needs. |
| Interoperability | The orchestrator (LangChain) and the compliance agent (Strands) talk over A2A without knowing each other's framework. |
| Composability | The compliance agent calls back to the enterprise's context agent for metadata it does not have, so the answer combines both organizations' data. |
| Submit then poll | The orchestrator's API returns a job ID at once (`POST /jobs`, HTTP 202) and the caller polls `GET /jobs/{job_id}`. A full request takes longer than the Amazon API Gateway integration limit of 30 seconds. |

### Components

| File | Account | Role | Framework | Model | Runs on |
|---|---|---|---|---|---|
| `agents/compliance_saas_agent.py` | SaaS provider | Compliance scanner. Publishes an Agent Card with one curated skill and calls back to the registered customer for context | Strands Agents (a2a-sdk 0.3) | Claude Sonnet 4.6 | Amazon Bedrock AgentCore Runtime, behind its JWT authorizer |
| `agents/enterprise_orchestrator.py` | Enterprise | Entry point. Discovers agents from their cards, lets the model route, and serves the jobs API | LangChain with ChatBedrock, FastAPI | Claude Sonnet 4.6 | Amazon ECS on AWS Fargate (port 8000), behind the internal Network Load Balancer (NLB, from Elastic Load Balancing) and the user API |
| `agents/enterprise_context_agent.py` | Enterprise | Enterprise metadata and escalation contacts; the SaaS agent's callback target | a2a-sdk 1.2.2 (v0.3 compatibility mode), LangChain | Amazon Nova Lite | Amazon ECS on AWS Fargate (port 9001), behind the internal NLB and the partner API |
| `cloudformation/enterprise-identity.yaml` | Enterprise | User pool, two resource servers, the `a2a-enterprise-orchestrator-saas` and `a2a-enterprise-orchestrator-user` clients, the empty orchestrator secret, and the reserved partner API | | | Stack `a2a-enterprise-identity` (notebook 1) |
| `cloudformation/enterprise-infra.yaml` | Enterprise | Amazon Virtual Private Cloud (Amazon VPC), internal NLB (cross-zone load balancing on), VPC link, the `saas-jwt` gate on the partner API, the user API, Amazon Elastic Container Registry (Amazon ECR), CodeBuild, ECS cluster, task roles, the orchestrator secret's resource policy | | | Stack `a2a-enterprise-infra` (notebook 3) |
| `cloudformation/enterprise-services.yaml` | Enterprise | ECS task definitions and services | | | Stack `a2a-enterprise-services` (notebook 3) |
| `cloudformation/saas-identity.yaml` | SaaS provider | User pool, the callback resource server, and the `a2a-saas-callback-client` client | | | Stack `a2a-saas-identity` (notebook 2) |
| `cloudformation/saas-provider.yaml` | SaaS provider | ECR repository, the empty callback secret and its resource policy, least-privilege AgentCore execution role, least-privilege CodeBuild role for the image build | | | Stack `a2a-saas-runtime` (notebook 2) |
| `docker/` | Enterprise | Hardened Dockerfiles and the CodeBuild buildspec for the two ECS images | | | CodeBuild |
| `1_enterprise_identity.ipynb`, `2_saas_provider.ipynb`, `3_enterprise_agents.ipynb` | Enterprise, SaaS provider, enterprise | The three numbered notebooks, generated by `build_notebook.py` | | | Jupyter |
| `build_notebook.py` | Both | Generates the three notebooks. Edit it, then run `python3 build_notebook.py`; never edit the `.ipynb` files by hand | | | Your machine |
| `requirements/` | Enterprise, SaaS provider | Exact dependency pins for the agent images | | | CodeBuild |
| `architecture-diagram.png` | Both | The architecture diagram at the top of [Architecture](#architecture), with the numbered steps | | | |

### Technology stack

| Technology | Role in this demo |
|---|---|
| **A2A protocol (v0.3)** | JSON-RPC 2.0 over HTTPS with a standard message format (`message/send`) and task lifecycle |
| **Agent Card** | JSON at `/.well-known/agent-card.json` describing the agent's endpoint `url`, its skills, and its `securitySchemes` |
| **OAuth 2.0 client credentials / JWT** | Each account's Cognito pool issues tokens only to its own workloads: `a2a-enterprise-orchestrator-saas` and `a2a-enterprise-orchestrator-user` in the enterprise pool, `a2a-saas-callback-client` in the SaaS pool. A workload exchanges its own client ID and secret at its own pool's `/oauth2/token` endpoint for a 15-minute access token with one scope, and sends it as a bearer token. The receiving gate checks the issuer, the client ID, and the scope |
| **Amazon API Gateway HTTP APIs** | HTTPS front doors in the enterprise account, each with a JWT authorizer, per-route scopes, throttling, and access logs. They reach the agents through a VPC link to an internal NLB |
| **Amazon Bedrock AgentCore Runtime** | Hosts the SaaS agent's A2A endpoint over HTTPS, with an inbound JWT authorizer that trusts the customer's pool (`discoveryUrl`, `allowedClients`, `allowedScopes`) |
| **Strands A2AServer, a2a-sdk** | A2A protocol handlers that serve the Agent Card and the JSON-RPC endpoint |
| **FastAPI, Starlette, httpx** | HTTP servers for the agents, and the HTTP client for outbound A2A calls |

## Prerequisites

- **Two AWS accounts in the same US Region**: one for the enterprise and one for the SaaS provider, both in us-east-1, us-east-2, or us-west-2.
- **Amazon Bedrock models in each account.** In commercial Regions, Amazon Bedrock enables these models on their first call, so there is no access request to submit ([Amazon Bedrock model access](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)):
  - SaaS provider account: Anthropic Claude Sonnet 4.6 (`us.anthropic.claude-sonnet-4-6`).
  - Enterprise account: Anthropic Claude Sonnet 4.6 and Amazon Nova Lite (`us.amazon.nova-lite-v1:0`).
  - Before the first call to Claude Sonnet 4.6 in an account, complete the Anthropic first-time use form (once per account, or once for the organization), and make sure the account has a valid payment method. The identity that makes the first call needs `aws-marketplace:Subscribe`, `aws-marketplace:Unsubscribe`, and `aws-marketplace:ViewSubscriptions`.
  - Each notebook checks its models in its first step, before anything is deployed. That check makes the first call in each account, so the deployer needs the AWS Marketplace permissions and the agents' roles don't.
- **Where to run** (either works; both need Python 3.10 or later):
  - One local Jupyter (`pip install jupyterlab`) with two AWS profiles. Put two files in the project folder, next to the three notebooks (the folder that holds `README.md` and `cloudformation/`): `.env.enterprise` with `AWS_PROFILE=<enterprise profile>` and `.env.saas` with `AWS_PROFILE=<SaaS profile>`, one line each. If you set `AWS_REGION=<region>`, set the same Region in both files. Notebooks 1 and 3 read `.env.enterprise`, and notebook 2 reads `.env.saas`. With one folder, the handoff files are already where the next notebook looks.
  - For local runs, install the AWS CLI v2 and use [AWS IAM Identity Center profiles](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html) (`aws configure sso`), so each deployer works with temporary credentials. Sign in to each profile with `aws sso login --profile <profile>` before you start.
  - Or an [Amazon SageMaker AI notebook instance](https://docs.aws.amazon.com/sagemaker/latest/dg/nbi.html) in each account. Notebooks 1 and 3 run on the enterprise instance, notebook 2 on the SaaS instance. Upload or unzip this folder on each. Choose the `conda_python3` kernel. The agents run Python 3.12 in their images.
  - You don't need Docker: AWS CodeBuild builds the agent images in each account.
  - The notebooks find the project folder by its files, so the unzipped folder can have any name.
- **Deployer permissions.** A sandbox administrator role covers all three notebooks. Otherwise, each notebook's Reference section lists the exact actions. Notebook 2 needs `iam:PassRole` for the stack's AgentCore execution role and CodeBuild role, and `iam:CreateServiceLinkedRole` for the AgentCore runtime identity service-linked role (`AWSServiceRoleForBedrockAgentCoreRuntimeIdentity`, service `runtime-identity.bedrock-agentcore.amazonaws.com`), which the first runtime in an account creates; notebook 3 needs it for the ECS execution and task roles and the CodeBuild role; notebook 1 needs none. No notebook needs `secretsmanager:GetSecretValue`.
- **Amazon Bedrock Guardrails permissions.** Notebooks 2 and 3 also use `bedrock:CreateGuardrail`, `CreateGuardrailVersion`, `GetGuardrail`, `DeleteGuardrail`, `ListTagsForResource`, and `TagResource` through CloudFormation, `bedrock:GetGuardrail` and `bedrock:ApplyGuardrail` on their own guardrail for the guardrail check, and `bedrock:ListGuardrails` and `bedrock:DeleteGuardrail` for the leftover checks and Clean up. The CloudFormation handlers also list KMS actions, which a sandbox administrator role covers.

## Deployment

Notebooks 1 and 3 run in the enterprise account, and notebook 2 runs in the SaaS provider account.

Run each notebook one cell at a time, top to bottom. Each step ends with a PASS line. Notebooks 2 and 3 end with a Clean up section, and a stop cell right before it ends a Run All there with a short message and no traceback. The only prompt in any notebook asks you to paste the previous step's one-line handoff, and only when its file isn't in the project folder.

| # | Account | Notebook | What it does | Time |
|---|---|---|---|---|
| 1 | Enterprise | `1_enterprise_identity.ipynb` | Creates the enterprise identity (pool, two app clients, the orchestrator's secret) and reserves the callback API. Writes `onboarding-request.json` | about 3 min |
| 2 | SaaS provider | `2_saas_provider.ipynb` | Validates the request, deploys the SaaS identity and this customer's AgentCore runtime, and checks that anonymous calls are denied. Writes `onboarding-response.json` | about 5 min |
| 3 | Enterprise | `3_enterprise_agents.ipynb` | Validates the response, calls the SaaS agent once, attaches the gate, deploys the agents, and runs the end-to-end test (E11) and the trust boundary checks (E12) | about 20 min |
| 4 | Both | The Clean up sections of notebooks 3 and 2 | Only after all testing: notebook 3's Clean up first, then notebook 2's. They delete everything the notebooks created | about 17 min |

The notebooks are generated by `build_notebook.py`. To change a notebook, edit the script and run `python3 build_notebook.py`; don't edit the `.ipynb` files by hand.

### The two handoffs

Two files move between the accounts. Copy each one into the other project folder, or paste the highlighted line the notebook shows at the end of I4 or S8. **Neither file contains a secret.**

| File | From, to | Fields |
|---|---|---|
| `onboarding-request.json` | Notebook 1 (enterprise) to notebook 2 (SaaS provider) | `issuer` and `discovery_url` (the enterprise pool's public OIDC issuer and discovery document), `token_endpoint`, `client_id` (the `a2a-enterprise-orchestrator-saas` client ID), `scope` (`a2a-enterprise-compliance-saas/invoke`), `callback_url` (the reserved partner API) |
| `onboarding-response.json` | Notebook 2 (SaaS provider) to notebook 3 (enterprise) | `saas_agent_card_url` (this customer's runtime card), `issuer` and `token_endpoint` (the SaaS pool), `callback_client_id` (the `a2a-saas-callback-client` client ID), `callback_scope` (`a2a-saas-customer-callback/invoke`), and echoes of the request's client ID and callback URL |

Both files also carry a `kind` and a `version`. Each receiving notebook validates every value before it deploys anything: https only, the expected AWS host names and Regions, an OIDC discovery document whose `issuer` matches, and, where it can tell, no value that points at its own account.

In production, the onboarding request and response move through the SaaS provider's onboarding portal or API, authenticated on both sides, instead of files.

After all testing, clean up both accounts: see [Clean up](#clean-up).

## Testing

The notebooks test the deployment as they go. Each step ends with a PASS line, and a failed check stops the run with a hint. The main checks:

| Step | What it shows |
|---|---|
| S3.1, E4.1 | Each account's guardrail is READY with the expected filters, blocks a prompt-attack probe, and allows texts like the ones the agents will see |
| S7.1 | Anonymous calls to the SaaS runtime, for the Agent Card and for `message/send`, get the runtime authorizer's 401 with a `WWW-Authenticate` header, before notebook 2 writes the onboarding response |
| E5 | Each gated enterprise route rejects a call with no token, before any agent starts |
| E9 | The context agent's public Agent Card answers anonymously, with the registered callback URL and the SaaS token endpoint and scope |
| E11 | The end-to-end test, below (about 1 to 3 min) |
| E12 | The trust boundary checks, below |

**E11, the end-to-end test.** Notebook 3, as the `a2a-enterprise-orchestrator-user` client, submits one question with `POST /jobs` and polls `GET /jobs/{job_id}`. The orchestrator calls the SaaS agent, which calls back to the context agent for enterprise context. E11 passes only if the reply has compliance findings from the SaaS agent's scan, at least one value that only the context agent knows (any on-call rotation code or channel from the context data, such as `PLAT-ONCALL-7` or `#data-oncall`), and the partner API access log shows a 200 for `POST /` from the SaaS callback client. Together they show that the question crossed both accounts, and that the callback reached the enterprise with a token the SaaS pool issued to its callback client.

**E12, the trust boundary checks.** E12 tries the wrong token at each gate (the partner API, the SaaS runtime, and the user API), then no token, then a token with a forged signature. All seven calls must be rejected. The trust boundary checks show that no token works at a gate it wasn't issued for. If check 3a or 3b is not rejected, stop and follow E12's text in notebook 3, which says what to do with the exposed token.

## Clean up

Clean up only after all testing, and the enterprise side first, so nothing calls the SaaS runtime while it is being removed: notebook 3's Clean up section, then notebook 2's (about 17 minutes in total, one after the other: notebook 3's takes about 12, notebook 2's about 5). Notebook 1 has no Clean up section, because notebook 3's covers everything notebooks 1 and 3 created.

Run All stops before the Clean up section, and setting DELETE_EVERYTHING = False makes the Clean up cells only print what they would delete. A stop cell right before each Clean up section ends a Run All there with a short message. Each Clean up section starts with a flag cell:

```python
DELETE_EVERYTHING = True
KEEP_ONBOARDING = False
```

Run the Clean up cells one at a time, top to bottom, only after all testing. Set `DELETE_EVERYTHING = False` to make them only print what they would delete. Leave `KEEP_ONBOARDING` False for a full clean up; set it to True only to retry a notebook (see [If a step failed](#if-a-step-failed)). The last cell checks what is left and fails if anything remains. If you restarted the kernel, first run every cell above S0 or E0 (Setup, Helpers, and Onboarding handoff helpers).

Enterprise account (`3_enterprise_agents.ipynb`, C1 to C8):

1. Stack `a2a-enterprise-services` (about 9 min, scaled to zero first)
2. Stack `a2a-enterprise-infra` (about 3 min: the gate on the partner API, the user API, the ECR repositories and images, the stack's log groups, the secret's resource policy, and the guardrail `a2a-enterprise-guardrail`)
3. Stack `a2a-enterprise-identity` (the pool, both clients, the reserved partner API). Deleting the pool stops new enterprise tokens; issued tokens expire within 15 minutes
4. Secret `a2a-enterprise/orchestrator-credentials`, with no recovery window
5. Bucket `a2a-enterprise-source-<account-id>-<region>`
6. Log groups `/aws/codebuild/a2a-enterprise-build` and `/aws/ecs/containerinsights/a2a-enterprise-cluster/performance`
7. The local `onboarding-request.json` and `onboarding-response.json`
8. A final check

SaaS provider account (`2_saas_provider.ipynb`, C1 to C10):

1. Stack `a2a-saas-runtime` (execution role, image-build role, the secret's resource policy, ECR repository and images, and the guardrail `a2a-saas-guardrail`)
2. `agentcore destroy --force` (about 4 min: runtime and the toolkit's CodeBuild project), then anything it left, through the AWS APIs. Its two "not found" warnings, for the ECR repository and the execution role, are expected, because item 1 already deleted both; the notebook labels them as expected
3. This agent's objects in `bedrock-agentcore-codebuild-sources-<account-id>-<region>`, and the bucket if it's empty; it is shared with other toolkit agents and otherwise stays
4. Log groups `/aws/bedrock-agentcore/runtimes/a2a_saas_compliance_agent-*` and `/aws/codebuild/bedrock-agentcore-a2a_saas_compliance_agent-builder`
5. Stack `a2a-saas-identity` (pool and callback client)
6. Secret `a2a-saas/callback-credentials`, with no recovery window
7. The local `agentcore_deploy/` folder
8. The local `onboarding-response.json`
9. The local `onboarding-request.json`
10. A final check

Item 2 also warns that it can't delete the image-build CodeBuild role: item 1 already deleted that role with the stack, and the notebook labels this warning as expected too. Notebook 2 passes the stack's CodeBuild role to the toolkit, so the toolkit creates no role of its own; item 2 still deletes one that an earlier run left.

## Cost estimate

### SaaS provider account

| Resource | Cost |
|---|---|
| Bedrock AgentCore Runtime | Pay per use (CPU and memory while a session is active); nothing while idle |
| Amazon Bedrock (Claude Sonnet 4.6) | Pay per token |
| Amazon Bedrock Guardrails | $0.15 per 1,000 text units (content filters, including prompt attack; a text unit is up to 1,000 characters); nothing while idle |
| CodeBuild | Per build minute (one build per launch) |
| ECR, Secrets Manager, CloudWatch Logs | Storage, one secret, and log ingestion: cents per day |

This account has almost no idle cost. Check the AWS pricing pages for your Region. Run the
Clean up section when you are done.

### Enterprise account

Approximate on-demand prices in us-east-1; check the AWS pricing pages for your Region.

| Resource | Cost |
|---|---|
| NAT gateway | about $0.045/hour plus data |
| Public IPv4 address on the NAT gateway | about $0.005/hour |
| ECS Fargate (2 tasks, 0.5 vCPU and 1 GB each) | about $0.05/hour |
| Network Load Balancer (internal) | about $0.0225/hour plus capacity units |
| VPC interface endpoints (2, each in 2 Availability Zones) | about $0.04/hour |
| API Gateway HTTP APIs | Pay per request |
| Amazon Bedrock (Claude Sonnet 4.6, Nova Lite) | Pay per token |
| Amazon Bedrock Guardrails | $0.15 per 1,000 text units (content filters, including prompt attack; a text unit is up to 1,000 characters); nothing while idle |
| Amazon Cognito (machine-to-machine tokens), Secrets Manager (one secret) | Cents per day |
| Container Insights metrics (enabled on the cluster) and CloudWatch Logs | Billed by use |
| **Idle total** | **about $0.16/hour (about $3.90/day)** |

Amazon Bedrock Guardrails: one end-to-end test (E11) uses about 40 to 60 text units across both accounts (an estimate), under one cent, and the guardrail checks S3.1 and E4.1 together about 32.

Run the Clean up section when you are done.

## Security controls

The sample applies these controls. [Considerations for production](#considerations-for-production) lists what it simplifies.

- **No secret crosses accounts.** Each pool issues tokens only to its own workloads, and each gate trusts the other pool by its public issuer, one client ID, and one scope. The onboarding files hold only public values. No notebook asks for a secret, and nobody copies one from a console.
- **JWT authorizers with exact pinning.** The AgentCore runtime pins the enterprise pool (`discoveryUrl`), the orchestrator client (`allowedClients`), and the scope (`allowedScopes`). The partner API's `saas-jwt` authorizer pins the SaaS issuer, the callback client ID, and the route scope, and the user API's `user-jwt` authorizer pins the enterprise issuer, the `a2a-enterprise-orchestrator-user` client, and the route scope. Every endpoint needs a JWT except the context agent's public Agent Card, which is static discovery metadata with no model call. FastAPI's `/docs`, `/redoc`, and `/openapi.json` are turned off.
- **Registered callback only.** The compliance agent sends its callback token only to the URL registered at onboarding (`CALLBACK_URL`), after it checks the public context card there. No caller, message field, header, or card can name another destination. A2A push notifications, which would let a caller name a webhook URL, are off on both servers.
- **Card pinning and curated skills.** The orchestrator sends its token only to the agent URL the card was fetched from. The SaaS card publishes one curated skill, not its internal tool names.
- **Amazon Bedrock Guardrails on every model call and on the other side's text.** Each account has one guardrail with a numbered version, created by CloudFormation: a prompt-attack filter at HIGH on input and harmful-content filters, with no sensitive-information filter, so the escalation contacts pass. An agent without a valid guardrail ID and numbered version makes no model call. The SaaS agent evaluates the orchestrator's message as `guardContent` and refuses non-text A2A parts; the context agent reads only the text parts of the callback request. Text that a model call can't evaluate is screened with `ApplyGuardrail` before any model sees it: the orchestrator screens the user's question, the SaaS Agent Card, and the SaaS reply, and the SaaS agent screens the context agent's reply. An intervention ends the request with a fixed message and logs only the policy names, never the text. Not covered: tool arguments a model generates, and, in the Classic tier, languages other than English, French, and Spanish, and prompt leakage.
- **Each secret has one reader.** Each secret is readable by exactly one workload role. The deployer reads app client secrets from Cognito, in memory only, to write the secret and to get test tokens. Each client secret is in its own account's AWS Secrets Manager, and only one workload role is granted `GetSecretValue` on it (`OrchestratorTaskRole` for `a2a-enterprise/orchestrator-credentials`, `SaasAgentRuntimeRole` for `a2a-saas/callback-credentials`). A resource policy on the secret denies `GetSecretValue` to every other principal. The policy guards against accidental and over-broad reads, not against a determined account administrator, who can remove it (CloudTrail records that). The enterprise secret gets its resource policy in notebook 3 (E4); until then only identity policies limit who can read it. No notebook reads a value back from Secrets Manager.
- **Tokens and secrets never shown.** No notebook output contains a client secret or a token. The AWS CloudFormation secrets are created empty, so a stack update can't overwrite a value. No secret is in code, images, CloudFormation parameters or outputs, environment values, or logs. Access tokens last 15 minutes, every notebook HTTP call refuses redirects, and the agents don't follow redirects either.
- **Caller-scoped jobs.** API Gateway overwrites a request header with the token's `client_id`, so a caller can't set it. Job IDs are random UUIDs, and `GET /jobs/{job_id}` returns 404 for a job another client submitted.
- **Least-privilege roles.** One task role per ECS agent and a stack-owned AgentCore execution role, each naming exact ARNs. Each agent's role can invoke only its own model: one inference-profile ARN in its account and Region, plus that model's foundation-model ARN with only the Region segment wildcarded, because the `us.` cross-Region profile can route a request to another US Region. The AgentCore role's trust policy accepts only Bedrock AgentCore in its own account and Region (`aws:SourceAccount`, `aws:SourceArn`), and it can pull one ECR repository, write its runtime's logs, and read one secret. `*` appears only for actions with no resource-level support (`ecr:GetAuthorizationToken`, AWS X-Ray writes, and namespace-conditioned `cloudwatch:PutMetricData`), each with a comment in the template. No workload role can pass roles or call Cognito, and the ECS task execution role can pull only the two agent repositories and write only their two log groups.
- **Private networking.** ECS tasks run in private subnets with no public IP, behind an internal Network Load Balancer (NLB). Security groups chain the VPC link, then the NLB, then each task, naming source security groups. Each task's only outbound rule is HTTPS, and the VPC endpoints' security group allows no outbound traffic. Bedrock and Secrets Manager traffic uses VPC interface endpoints (AWS PrivateLink).
- **TLS and storage.** Every cross-account call uses HTTPS (API Gateway and AgentCore). The enterprise source bucket in Amazon Simple Storage Service (Amazon S3) has all four S3 Block Public Access settings, a policy that denies non-TLS requests, default encryption, and names this account as the expected bucket owner on every call after the bucket is created (`create_bucket` can't take it).
- **Hardened containers.** The images use the `python:3.12-slim` base image from Amazon ECR Public, pinned by its multi-arch index digest (one digest for the x86_64 ECS images and the ARM64 AgentCore image; to bump it, take the top-level digest from `docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim`). They run as the unprivileged `appuser` (uid 10001), apply OS updates at build time, and have pip removed. Both ECS tasks run with a read-only root file system, every Linux capability dropped, and a `/tmp` bind mount on the task's ephemeral storage as the only writable path. AgentCore Runtime is managed and has no read-only root or capability setting; the SaaS agent relies on its microVM, the non-root user, the pinned base image, and configuration through environment variables only. The agents bind to `127.0.0.1` unless `BIND_HOST` is set; the images set it so the container network can reach them, and no host port is published. All three ECR repositories scan images on push, and the images install exact dependency pins (`requirements/`).
- **Fail closed, generic errors.** Missing or invalid credentials, card settings, or scopes mean the call isn't sent. Callers raise on JSON-RPC errors. Remote callers get generic error messages, and details go to logs only. Questions are limited to 1 to 4,000 characters, and both onboarding files are validated before anything is deployed.
- **Bounded network calls.** Every network call has an explicit connect and read timeout. The AWS SDK's standard retry mode, with a fixed number of attempts and jittered backoff, is the only retry layer for every AWS call (Strands' own model retry is off). A2A calls and the callback are never repeated, except once with a fresh token after a 401, so a retry can't run a scan or a model call twice. Every notebook wait has a limit, and the stack and build creates carry idempotency tokens.
- **Throttling and traceability.** Both HTTP API stages have throttling and access logs. Every cross-account call logs the JSON-RPC ID, the A2A task ID, and the AWS request IDs (`x-amzn-requestid`, `apigw-requestid`), so each side can correlate a call with its own logs.
- **No users, synthetic data.** Cognito self sign-up is disabled (`AllowAdminCreateUserOnly`), and the pools have no users. The data is synthetic: no real account IDs, personal data, or credentials.

## Considerations for production

The sample keeps the setup small. Before you use these patterns in production, address each area below.

| Area | Demo behavior | Production guidance |
|---|---|---|
| Customers | The SaaS provider serves one customer, with one runtime and one callback client | One runtime per customer, or a front door (below) |
| Onboarding | The request and response are unsigned files, validated field by field | The SaaS provider's authenticated onboarding portal or API |
| Reserved callback API | Between notebook 1 and notebook 3, the partner API has no routes and no stage, so nothing is reachable through it | Keep it this way: reserve the URL first, attach the gate before any agent serves |
| Job store | Jobs are kept in memory in the single orchestrator task (`DesiredCount: 1`), up to 100 jobs, finished jobs expire 1 hour after submission, and all jobs are lost on restart | A DynamoDB job store, keyed by job ID with the caller's client ID as an attribute and a TTL, so the orchestrator can run more than one task |
| Callback time limit | The callback goes through API Gateway, whose HTTP API integration timeout is 30 seconds and can't be raised. The context agent's reply is short, so it fits. The context agent's timeouts apply per model call, and a callback usually makes two model calls, so a sustained Amazon Bedrock throttle can exceed the 30 seconds, and the SaaS agent then reports the enterprise context as unavailable | Keep callbacks short, or make them asynchronous like the jobs API |
| Agent discovery | The orchestrator discovers agents once, at startup. A card that it can't fetch or screen is dropped until its task restarts; E10 detects this (see [Troubleshooting](#troubleshooting)) | Retry discovery, and have `/health` report unhealthy when no tools were built, so the NLB health check fails and ECS replaces the task |
| Callers | Notebook 3 stands in for the enterprise app, using the `a2a-enterprise-orchestrator-user` client-credentials client | People sign in with the authorization-code flow; each app gets its own client |
| Client authentication | Each workload authenticates to its own pool with a client secret | Identity providers that support key-based client authentication (`private_key_jwt` or mutual TLS), so no workload holds a shared client secret |
| Encryption keys | S3, ECR, Secrets Manager, CloudWatch Logs, and Amazon Bedrock Guardrails use service-default encryption | Customer managed AWS KMS keys, with key policies that allow only the roles that need each key |
| Network logging | VPC Flow Logs and NLB access logs are off; both API stages have access logs | Turn on VPC Flow Logs and NLB access logs |
| Image tags | Mutable `:latest` tags, re-pushed on each run; scan on push is on | Immutable, versioned tags |
| Inside the VPC | TLS terminates at API Gateway. The last hop (VPC link, then the internal NLB's TCP listeners, then the containers) is plain HTTP over TCP on private addresses in private subnets, reachable only through the security-group chain (VPC link SG, then NLB SG, then each task's SG) | If your policy requires encryption inside the VPC, add a TLS listener on the NLB with an ACM certificate, or terminate TLS in the containers, and set the integration's TLS server name. A public ACM certificate needs a domain you own. AWS Private CA avoids a public domain, but API Gateway checks a private integration's certificate against [its supported certificate authorities](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-supported-certificate-authorities-for-http-endpoints.html), so confirm Private CA support before you rely on it |
| Shared roles | One ECS task execution role for both agents, limited to pulling the two agent repositories and writing their two log groups; ECS and CodeBuild trust policies have no `aws:SourceAccount` condition | Separate execution roles; add confused-deputy conditions where the service supports them |
| Context agent tasks | Any holder of a valid `a2a-saas-callback-client` token can call `tasks/get` on the context agent; only that client passes the partner API | Scope task lookups to the caller |
| Callback data | The callback sends a fixed request with no user prompt, and the context agent returns metadata and generic errors | Filter the returned fields per caller |

Also plan for these:

- **One runtime per customer, or a front door that accepts several customer identity providers.** An AgentCore runtime takes one `discoveryUrl`. To serve many customers from one endpoint, put a front door in front that validates tokens from each registered customer's identity provider and routes by issuer.
- **Per-customer callback clients.** Issue one callback client per customer in the SaaS pool, and have each customer's partner API accept only its own client ID, so a callback token for one customer can't be replayed against another. Likewise, give each SaaS provider its own caller client and scope.
- **Client-secret rotation within each account.** Cognito supports two active secrets per app client ([app client secrets](https://docs.aws.amazon.com/cognito/latest/developerguide/user-pool-settings-client-apps.html)). Add a second secret (`AddUserPoolClientSecret`), write it to that account's own Secrets Manager secret, then delete the old one (`DeleteUserPoolClientSecret`). Nothing changes in the other account. The SaaS agent re-reads its secret within 5 minutes or on a 401; the orchestrator reads its secret at startup, so it needs a new task.
- **AWS PrivateLink for private connectivity.** For the enterprise's calls to the SaaS agent, use an [AgentCore interface VPC endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-interface-endpoints.html). HTTP APIs can't be private, so for a private callback run the runtime in AgentCore VPC mode and use a [private REST API](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-apis.html) through an `execute-api` interface endpoint in the SaaS VPC, with a resource policy that names that endpoint and a Lambda authorizer that pins the same issuer, client ID, and scope as `saas-jwt`. Another option is to validate the JWT in the context agent itself. Don't put an endpoint service directly in front of the NLB, because that skips the JWT check. Enterprise egress must allow its own Cognito domain and `bedrock-agentcore.<region>.amazonaws.com` (or the interface endpoint). With the public callback API, SaaS egress must allow its own Cognito domain and `<api-id>.execute-api.<region>.amazonaws.com`.
- **Custom domains** with your own certificates on the APIs ([custom domain names for HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-custom-domain-names.html)), so callers and the registered callback URL don't depend on the `execute-api` host name.
- **AWS WAF through a REST API or Amazon CloudFront, not an HTTP API.** AWS WAF can protect an API Gateway REST API but can't be attached to an HTTP API ([choose between REST APIs and HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-vs-rest.html)). Use a REST API for that endpoint ([AWS WAF for REST APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-control-access-aws-waf.html)), or put CloudFront with an AWS WAF web ACL in front and close off direct access to the API's own endpoint.
- **Amazon Bedrock Guardrails at production strength** ([guardrails](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html)). The demo's guardrails use the Classic tier and the AWS managed key. In production, use the Standard tier with a guardrail profile (cross-Region inference), a customer managed AWS KMS key, `bedrock:GuardrailIdentifier` conditions in the agents' policies so no model call can skip the guardrail, and sensitive-information filters where masking is acceptable. Keep treating the other side's card, skill text, and replies as untrusted model input.
- **Contact details that pass through unchanged.** In one of three live E11 runs on 2026-10-28, a model in the chain retyped one escalation contact instead of passing it through unchanged. E11 still passed, because it accepts any one context-only marker. In production, have the context source return structured data that callers pass through unchanged, check contact details against the system of record, and keep a person in the loop for escalations.
- **Your own security scans** before you deploy to any shared environment, for example cfn-lint and Checkov on the templates, Bandit on the Python code, and Trivy on the repository and images.

## Troubleshooting

### If a step failed

Every step refuses resources that already exist, so a failed step can't be fixed by running its cell again. Run the notebook's "Recent logs" section first, then:

| What failed | What to do | Notebooks that run again |
|---|---|---|
| Notebook 3 | In notebook 3, set `KEEP_ONBOARDING` to True (`DELETE_EVERYTHING` is already True), run the Clean up cells, set `KEEP_ONBOARDING` back to False, and run notebook 3 again from the top. Notebook 1's identity and both handoff files are kept | 3 |
| Notebook 2 | The same in notebook 2. `onboarding-request.json` is kept, so no paste is needed. If notebook 3 had already run, retry it the same way with the new `onboarding-response.json` | 2 (then 3) |
| Notebook 1 | In notebook 3, run every cell above E0 (Setup, Helpers, and Onboarding handoff helpers), then its Clean up with `KEEP_ONBOARDING` False, then run notebook 1 again from the top | 1 |
| Start over | Notebook 3's Clean up, then notebook 2's, both with `KEEP_ONBOARDING` False, then notebooks 1, 2, and 3 in order | 1, 2, 3 |

### E10 finds no SaaS agent

The orchestrator discovers agents once, at startup. It drops a card that it can't fetch or screen, and the card stays dropped until the task restarts. Read the orchestrator's log in notebook 3's "Recent logs" section. If it shows `Failed to fetch Agent Card` or `Rejected Agent Card ... guardrail check failed`, start a new deployment of the orchestrator (`aws ecs update-service --cluster a2a-enterprise-cluster --service orchestrator --force-new-deployment`), then rerun E8 and E10. E10's hint lists the other causes.

### Guardrail tuning

If a guardrail blocks a legitimate run, the agent's WARNING line names the stage and the policies (`<input|output>:<type>:<action>`). Lower that one filter's strength by one level in that account's template (`cloudformation/enterprise-infra.yaml` or `cloudformation/saas-provider.yaml`), make the same edit in `EXPECTED_GUARDRAIL_FILTERS` in `build_notebook.py` and in the version's Description, regenerate the notebooks, and redeploy that side: about 32 minutes for the enterprise side and 42 for the SaaS side. Each notebook's Reference section (Guardrail tuning) has the steps.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
