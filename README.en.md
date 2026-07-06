# JetUse on OCI — Generative AI Use-Case Platform (Public edition)

A web app prototype that bundles internal generative-AI use cases on top of OCI Enterprise AI
(OpenAI-compatible agentic API): chat / use-case engine / RAG / DB chat (NL2SQL) /
agents (multiple frameworks) / voice (minutes, live transcription, voice chat) /
image & video analysis — all on OCI managed services.

> [日本語 README](./README.md) ｜ Architecture: [docs/architecture/system.md](./docs/architecture/system.md)

## Deploy to Oracle Cloud

[![Deploy JetUse to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/sogawa-yk/jetuse/releases/download/orm-main/jetuse-orm.zip)

One Resource Manager stack contains both IAM and the JetUse application. No working directory is required. Select the IAM controls according to the executing user's permissions:

- Tenancy IAM administrator: leave `enable_dynamic_group` and `enable_runtime_policy` enabled.
- Existing dynamic groups: disable `enable_dynamic_group` and create only the compartment runtime policy.
- All IAM pre-created: disable both controls and create only application resources.

An IAM operation fails during plan or apply when the executing user lacks its permission. End users sign in through the generated OIDC application and require no OCI IAM permissions. See [the Resource Manager guide](./docs/setup/orm.md) and [the IAM guide](./docs/setup/iam.md).

The public images live in the Osaka OCIR (`kix.ocir.io`) and OCI Functions only accepts images from an OCIR in its own region. Outside Osaka the stack therefore **skips the Functions router automatically** — the affected API routes (`presets`/`dbchat`/`tts`) fall back to the Container Instance through the gateway's catch-all route, which has a 60s read timeout (chat SSE on `/api/chat/*` keeps its 300s route and is unaffected). To use the router elsewhere, mirror the image to your region's OCIR and set `fn_router_image`. A full apply outside Osaka has not been exercised yet (Issue #55 / ADR-0017).

## Features

| Area | Capability |
|---|---|
| Chat | Streaming, model selection, params/presets, short-term memory, Markdown/Mermaid |
| Use cases | Form + prompt-template builder & sharing, 5 built-ins |
| RAG | Upload docs → cited answers (Vector Store / Select AI backends) |
| DB chat | NL → SQL generate & run (SQL Search / Select AI), result charting |
| Agents | Tools, MCP, memory isolation. Engine: **native / OpenAI Agents SDK (default) / LangGraph** |
| Voice | Minutes (diarization), live transcription, half-duplex voice chat |
| Multimodal | Image-input chat, video frame analysis |
| Admin/Ops | Audit log & usage dashboard, input moderation, rate limiting, OCI Logging/Monitoring |

## Architecture

- **Frontend**: React SPA (Object Storage static hosting + API Gateway, HashRouter)
- **API**: SSE = Container Instance (FastAPI) / non-streaming = OCI Functions (ADR-0005)
- **AI**: OCI Enterprise AI (OpenAI-compatible Responses/Chat Completions, IAM signing), Osaka region
- **Data**: ADB 26ai, Object Storage
- **Auth**: IAM Identity Domain (OIDC + PKCE), SAML federation guide included
- Details & Mermaid diagram → [docs/architecture/system.md](./docs/architecture/system.md)

## Layout

```
packages/web/    React SPA
packages/api/    FastAPI(service/) + Functions router(fn/) + shared logic(jetuse_core/)
infra/terraform/ Terraform modules (environments/dev is the live env)
docs/            plan / decisions(ADR) / verification / comparison/ / guides/ / setup / tips
specs/           feature specs per phase
```

## Deploy

Prereqs: OCI tenancy (Osaka recommended), `~/.oci/config`, Terraform 1.15+ / Node 22 /
Python 3.12 / podman. Put env-specific values in `.env` (template `.env.example`) — never
commit credentials/OCIDs/endpoints. Human prerequisites: IAM dynamic group & policy
(`docs/setup/iam.md`) and Identity Domain (`specs/06`).

```bash
# Infra
cd infra/terraform/environments/dev && terraform init && terraform apply
cd packages/api && python -m jetuse_core.migrate    # ADB migrations

# API image (SSE / Container Instance)
podman build -t <region>.ocir.io/<ns>/jetuse-dev-api:<ver> . && podman push <...>
# → update api_image_url in tfvars, terraform apply

# Functions router (non-streaming)
podman build -f Containerfile.fn -t <region>.ocir.io/<ns>/jetuse-dev-fn-router:<ver> . && podman push <...>
# → update fn_router_image in tfvars, terraform apply

# SPA
cd packages/web && npm install && npm run build && bash scripts/deploy.sh
```

Operational gotchas (see [docs/tips.md](./docs/tips.md)): recreating the CI to inject env vars
does **not** redeploy code if the image tag is unchanged; OCIR push can fail silently under disk
pressure (verify the version exists in the registry before apply); the dev ADB gets caught by
nightly stops (start it before working).

## Development

```bash
cd packages/api && AUTH_REQUIRED=false uvicorn service.main:app --port 8000
cd packages/web && VITE_AUTH_REQUIRED=false npm run dev   # proxies /api to :8000
```
Before commit: `ruff check . && pytest` (API) / `npm run build && npm run lint` (web).

## Docs

See [docs/architecture/system.md](./docs/architecture/system.md), [docs/comparison/aws-reference.md](./docs/comparison/aws-reference.md),
the `docs/comparison/` selection studies, [docs/guides/customize.md](./docs/guides/customize.md),
[docs/guides/demo-scenarios.md](./docs/guides/demo-scenarios.md), and [docs/tips.md](./docs/tips.md).

## License / status

`main` is the formal Public edition and `dev` is the next Internal release line. See the repository license for usage terms.
