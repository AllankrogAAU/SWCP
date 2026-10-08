# SWCP

SWCP is a prototype code-submission and LLM feedback platform. The repository
contains the Vue frontend, stateless ASP.NET API gateway, ASP.NET Core domain
API, JetStream-based LLM worker, Python sandbox worker, and optional local vLLM
server.

## Requirements

- Docker Desktop with Linux containers and Docker Compose.
- Node.js and npm to run the Vue frontend with Vite.
- NVIDIA/CUDA-compatible Docker runtime and local Hugging Face model files only
  when using `local-llm`.

Create the root `.env` from `.env.example` and replace the local development
passwords. Do not commit `.env`. The Core API generates an RS256 key pair into
separate named volumes: its private key stays mounted only in Core, and the
gateway receives the public key read-only. Development teacher seeding is
optional and controlled by `SEED_DEVELOPMENT_USERS`, `DEV_TEACHER_EMAIL`, and
`DEV_TEACHER_PASSWORD`.

## Run The System

From the repository root, start gateway, Core API, LLM worker, sandbox worker,
NATS JetStream, PostgreSQL, and pgAdmin:

```powershell
docker compose up -d --build
```

The optional profiles add the frontend and local vLLM server:

```powershell
docker compose --profile frontend up -d --build
docker compose --profile local-llm up -d --build
docker compose --profile frontend --profile local-llm up -d --build
```

The public gateway is at `http://localhost:5170`; Scalar is available in
Development at `/scalar/v1`. The frontend container is served at
`http://localhost:5174`; Vite development uses `http://localhost:5173`. pgAdmin
is at `http://localhost:5050`. NATS client and monitoring ports are 4222 and
8222. Compose's default `down` preserves database, NATS, and signing-key volumes;
avoid `docker compose down -v` unless you intend to delete them.

## Frontend Development

```powershell
cd apps/frontend
npm ci
npm run dev
```

Vite proxies `/api` and `/ws` to the gateway. The Nginx container uses equivalent
proxies when the `frontend` Compose profile is enabled. Frontend checks:

```powershell
npm run build
npm run test:unit -- --run
```

## Prototype Flow

1. Register a student through `POST /api/auth/register`, then log in through
	`POST /api/auth/login`.
2. Load assignments through `GET /api/assignments/`.
3. Submit source to `POST /api/submissions/` with an assignment ID and
	`llmBackend` (`azure` or `local`). The Core API stores the submission and
	publishes a `sandbox.tasks` JetStream task, returning `202` and its ID.
4. The trusted sandbox worker consumes the task and calls the
	existing `apps/cSandbox/evaluation/evaluator.py` without changing evaluator
	or sandbox internals. It publishes `sandbox.results`.
5. Core stores compiler/runtime output. Compile failures complete immediately;
	successful compilation builds a prompt and publishes `llm.tasks.azure` or
	`llm.tasks.local`.
6. `llmWorker` calls Azure AI Foundry or the OpenAI-compatible local vLLM API,
	publishes `llm.results`, and Core stores final feedback.
7. The frontend receives `notifications.<submissionId>` over WebSocket and also
	polls `GET /api/submissions/{id}` as a fallback.

The worker adapter accepts compiler flags and test cases, but the existing
evaluator currently performs one compile and one run. The adapter reports that
limitation rather than changing the sandbox logic. The sandbox worker mounts
the Docker socket so its adapter can create isolated per-submission containers;
this is a development-only privilege and should be replaced by the planned
Kubernetes/gVisor worker deployment later.

## Local Model

The `local-llm` profile starts vLLM and uses the full Hugging Face model
directory already under `apps/localLLM/models`; it does not download weights.
`LLM_MODEL_PATH` selects that directory and `LLM_MODEL_NAME` is the served API
name. GPU tuning is controlled by the `VLLM_*` settings in `.env`. Select
`llmBackend: "local"` for a submission to use this worker path.
If `llmBackend` is `local`, start the `local-llm` profile first. Azure requests
use the deployment configured as `AzureAIFoundry:Models:Gpt4o:Deployment`; a
provider 404 usually indicates the configured endpoint/deployment does not
exist or is not available in that Azure resource.

## Validate Builds

```powershell
dotnet tool restore
dotnet build apps/apiGateway/api.slnx
docker compose config --quiet
docker compose --profile frontend --profile local-llm build
```

The local vLLM model files are bind-mounted and excluded from application image
build contexts.