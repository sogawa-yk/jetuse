# JetUse on OCI — Generative AI Use-Case Platform (Public edition)

A web app prototype built on OCI Enterprise AI (OpenAI-compatible agentic API) that bundles chat,
use cases, RAG, DB chat (NL2SQL), agents, voice, and image/video analysis into one app —
all running on OCI managed services.

[日本語 README](./README.md)

## Deploy

[![Deploy JetUse to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=https://github.com/sogawa-yk/jetuse/releases/download/orm-main/jetuse-orm.zip)

The button hands one Terraform stack — IAM plus the application — to OCI Resource Manager (no working
directory needed). It builds VCN / Autonomous Database / API Gateway / Container Instance / Functions /
Object Storage / Identity Domain, and you sign in to the `app_url` output with `demo_username` /
`demo_password`. The first apply takes 10–15 minutes.

- Inputs are essentially the target compartment and `prefix`. Passwords are generated; images come from the public OCIR.
- Toggle `enable_dynamic_group` / `enable_runtime_policy` to match the executing user's IAM permissions.
- Supported regions, service limits, and the pre-deploy checklist: [Resource Manager guide](./docs/setup/orm.md).
  Required permissions: [Public IAM requirements](./docs/setup/public-iam-requirements.md) and the [IAM guide](./docs/setup/iam.md).

## Features

| Area | Capability |
|---|---|
| Chat | Streaming, model selection, params/presets, short-term memory, Markdown/Mermaid |
| Use cases | Form + prompt-template builder & sharing, 5 built-ins |
| RAG | Upload docs → cited answers (Vector Store / Select AI backends) |
| DB chat | NL → SQL generate & run (SQL Search / Select AI), result charting |
| Agents | Tools, MCP, memory isolation. Engine: native / OpenAI Agents SDK (default) / LangGraph |
| Voice | Minutes (diarization), live transcription, half-duplex voice chat |
| Multimodal | Image-input chat, video frame analysis |
| Admin/Ops | Audit log & usage dashboard, input moderation, rate limiting, OCI Logging/Monitoring |

## Architecture

- **Frontend**: React SPA (Object Storage static hosting + API Gateway, HashRouter)
- **API**: SSE = Container Instance (FastAPI) / non-streaming = OCI Functions (ADR-0005)
- **AI**: OCI Enterprise AI (OpenAI-compatible Responses/Chat Completions, IAM signing)
- **Data**: ADB 26ai (conversations, definitions, minutes, NL2SQL), Object Storage (docs, audio, wallet)
- **Auth**: IAM Identity Domain (OIDC + PKCE), SAML federation guide included

Details & Mermaid diagram → [docs/architecture/system.md](./docs/architecture/system.md)

## Development

Setup through your own cloud E2E environment: [onboarding guide](./docs/guides/onboarding.md) (Japanese).

```bash
cd packages/api && AUTH_REQUIRED=false uvicorn service.main:app --port 8000  # API (auth off)
cd packages/web && VITE_AUTH_REQUIRED=false npm run dev                     # SPA (proxies /api to :8000)

make lint && make test && make build   # before commit (entry point = root Makefile; see make help)
make deploy DEV=<name>                  # deploy to your own OCI environment for E2E
```

```
packages/web/    React SPA
packages/api/    FastAPI(service/) + Functions router(fn/) + shared logic(jetuse_core/)
infra/           terraform/(modules and environments) + orm/(one-click stack)
docs/            design, ADRs, verification reports, operational guides
specs/           feature specs per phase
```

Branches: `main` (stable Public edition, source of the Deploy button) / `public-dev` (Public integration) /
`internal-dev` / `internal-stable`. Public changes land on `public-dev` and reach `main` at release time
([branching and releases](./docs/guides/branching-and-releases.md)). Verification is done against real OCI
environments; reports live in `docs/verification/`.

## Docs

Index: [docs/README.md](./docs/README.md). Frequently used:

| Topic | Where |
|---|---|
| Overall design & diagrams | [docs/architecture/system.md](./docs/architecture/system.md) |
| Why a design was chosen | [docs/decisions/](./docs/decisions/) (ADR) |
| Option studies (RAG/NL2SQL/agent FW/compute) | [docs/comparison/](./docs/comparison/) |
| Customization | [docs/guides/customize.md](./docs/guides/customize.md) |
| Demo scripts | [docs/guides/demo-scenarios.md](./docs/guides/demo-scenarios.md) |
| Real-world gotchas | [docs/tips.md](./docs/tips.md) |
