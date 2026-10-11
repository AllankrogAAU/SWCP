# SWCP

## Prerequisites

- Docker Desktop with Linux containers and Docker Compose.
- Node.js 24.12+ (or 22.18+) and npm to run the frontend directly.
- A CUDA/NVIDIA-enabled Docker runtime and the local model files when using
	the `local-llm` profile.

## Environment

Do not commit `.env`.

## Run The Stack

From the repository root, start the API gateway, cloud LLM worker, NATS
JetStream, database, and pgAdmin:

```powershell
docker compose up -d --build
```

Add optional services with Compose profiles. These commands also start the
default services above:

```powershell
docker compose --profile frontend up -d --build
docker compose --profile local-llm up -d --build
docker compose --profile frontend --profile local-llm up -d --build
```

`docker compose down` preserves database and JetStream data volumes. Avoid
`docker compose down -v` unless you intend to delete that data.

The sandbox runtime is available as an opt-in service for Compose validation:

```powershell
docker compose --profile sandbox up -d --build
```

It stays out of the default stack because it is a runtime image for
pre-compiled binaries rather than a long-running application service. The
runner applies the isolation settings required for executing submissions.

## Frontend Development

For Vite hot reload, run the frontend outside Docker:

```powershell
cd apps/frontend
npm ci
npm run dev
```

## Local LLM

The `local-llm` profile uses the existing vLLM image and local model files; it
does not download model weights. The CI Trivy scan explicitly ignores the
currently unfixed upstream vLLM CVEs listed in `.trivyignore-vllm`.
Set `LLM_MODEL_PATH` and `LLM_MODEL_NAME` in the root `.env`. The model path names a directory under
`apps/localLLM/models`, for example `Qwen2.5-Coder-1.5B-Instruct-AWQ`. That
directory must contain the complete Hugging Face model repository required by
vLLM. Adjust the `VLLM_*` settings in `.env` for your GPU and workload.

## Test And Validate Containers

CI validates the Compose service graph and runs a deterministic smoke test for
the API and frontend. The optional local LLM profile is not required for CI;
it needs a locally available model and a CUDA-capable runtime.

## Delivery validation

The delivery workflow is called after the `ci-gate` job
passes on pushes to `main` or after a manual workflow dispatch. It:

1. Builds immutable images tagged with the commit SHA and publishes them to
   GHCR.
2. Starts an  Compose staging environment using those exact images.
3. Runs API/frontend smoke tests, Playwright, and a ZAP baseline scan.
4. Uploads the Playwright and ZAP reports.
5. Adds `staging-verified-<sha>` tags to the images only after all staging
   verification succeeds.

This is delivery to an ephemeral staging environment, not deployment to a
persistent production host. The production target remains intentionally
provider-neutral for this project.

test addition