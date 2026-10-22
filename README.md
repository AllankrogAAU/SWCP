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

The sandbox runtime is available as an opt-in service for Compose validation:

```powershell
docker compose --profile sandbox up -d --build
```

It stays out of the default stack because it is a runtime image for
pre-compiled binaries rather than a long-running application service. The
runner applies the isolation settings required for executing submissions.

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

## Frontend Benchmark

`apps/benchmark` contains a streaming CodeNet sampler, a Playwright driver that
submits through the actual frontend, resumable JSONL telemetry, an optional
Azure feedback judge, a Cohen's kappa calculator, and dependency-free SVG/LaTeX
report generation. It does not extract the CodeNet archive or download model
weights. The separate repository-fixture validation slice is included unless
`--no-repo-fixtures` is passed.

Install the benchmark dependencies and Chromium once:

```powershell
python -m venv .venv-benchmark
.\.venv-benchmark\Scripts\Activate.ps1
python -m pip install -r apps\benchmark\requirements.txt
python -m playwright install chromium
```

Set `SWCP_BENCHMARK_TEACHER_USERNAME`, `SWCP_BENCHMARK_TEACHER_PASSWORD`, and
`SWCP_BENCHMARK_STUDENT_PASSWORD` in the shell or root `.env`. The runner passes
the teacher values to Compose with development seeding enabled so it can
provision CodeNet assignments. The teacher username/password must match an
existing seeded account if that account is already present in the database.
Keep these values private; benchmark result JSONL also contains submitted C
source and model feedback and is ignored by Git.

Start with a small validation run before the default 20-per-status sample:

```powershell
$env:SWCP_CODENET_ARCHIVE = 'C:\Users\allan\Downloads\Project_CodeNet.tar.gz'
python apps\benchmark\src\run_benchmark.py `
  --samples-per-status 1 --no-repo-fixtures --model azure:Gpt6Sol
```

The runner starts/rebuilds the existing Compose frontend and services, uses
`POSTGRES_PORT=15432` by default to avoid the known host port 5432 conflict,
creates assignments as needed, then submits each sample through Playwright.
Use `--model azure:Gpt6Sol --model azure:Gpt4o` to run both configured Azure
deployments, or repeat `--model local:<relative-model-directory>[=<served-name>]`
to run local models. Local paths must already exist under
`apps/localLLM/models` with `config.json` and weight files; no download is
attempted. For local models the runner configures vLLM on the Compose host port
(8080 by default), with 0.92 GPU memory utilization, 4096 context length, and
four sequences. It uses AWQ-Marlin above 7B and eager mode at 12B or larger.
Use `--burst-concurrency N` for parallel browser sessions with distinct
benchmark student accounts; the default is sequential. The Compose model
service is `local-llm-server` on port 8080 in this repository.

`apps/benchmark/results/inferences.jsonl` is append-only and flushed after each
inference. Successful `(sample, provider, model, context)` rows are skipped on
resume; failed rows can be retried. Pipeline stage timing and token usage come
from structured worker logs. Estimated cost remains null unless both per-million
input/output rates are supplied with `--input-usd-per-million` and
`--output-usd-per-million`; use `--pricing-json` for different rates by model,
with keys such as `Azure:Gpt6Sol` and fields `input_usd_per_million` and
`output_usd_per_million`. `peak_vram_mb` is sampled from `nvidia-smi` during
each local inference (and represents total device usage, not a process-only
allocation).

The UI currently sends WebSocket progress/status events, not generated content
tokens. Accordingly, `frontend_ttft_ms` means time to the first WebSocket
progress event, not model time-to-first-token. Pipeline phase values derived
from browser events are approximate; structured worker stage durations and
queue waits are preferred when present.

After collecting results, optionally run the Azure judge and generate
publication assets:

```powershell
python apps\benchmark\src\judge_feedback.py apps\benchmark\results\inferences.jsonl
python apps\benchmark\src\calibration.py apps\benchmark\results\judged.jsonl
python apps\benchmark\src\report.py apps\benchmark\results\judged.jsonl
```

To produce QoS-only plots without invoking the optional judge, pass
`inferences.jsonl` to `report.py` instead.

The judge reads `SWCP_BENCHMARK_JUDGE_ENDPOINT`,
`SWCP_BENCHMARK_JUDGE_API_KEY`, and `SWCP_BENCHMARK_JUDGE_DEPLOYMENT`. Endpoint
and API key can fall back to the configured Gpt4o Azure settings in `.env`; set
the judge deployment explicitly. Judge inference charges are separate and are
not included in benchmark model cost estimates. Human calibration labels can be
added as `human_label` values to judged JSONL before computing Cohen's kappa.
SVG charts, summary JSON, and a LaTeX table are written under
`apps/benchmark/results/plots`.

For a readable timing breakdown of the latest individual submission:

```powershell
.\scripts\Show-PipelineTimings.ps1
```
