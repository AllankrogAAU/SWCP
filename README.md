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

TBD

test addition