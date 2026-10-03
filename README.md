# SWCP

## Prerequisites

- Docker Desktop with Linux containers and Docker Compose.
- Node.js 24.12+ (or 22.18+) and npm to run the frontend directly.
- A CUDA/NVIDIA-enabled Docker runtime and the local model files only when using
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

The API gateway is on `http://localhost:5170`; Scalar is at
`http://localhost:5170/scalar/v1`. pgAdmin is on `http://localhost:5050`, and
NATS client/monitoring ports are `4222`/`8222`. Change host ports in the root
`.env` if they conflict. The cloud LLM worker is background-only and does not
publish an HTTP port.

The local LLM proxy is part of the API gateway. With the `local-llm` profile
enabled, use `/api/local-llm/prompt` or `/api/local-llm/code-analysis` on the
gateway; vLLM itself is also available on `http://localhost:8080`.

Cloud analysis is asynchronous: authenticated requests to
`POST /api/cloud-llm/analysis/` or `POST /api/cloud-llm/{category}/analyze`
return `202` with a job ID. Poll `GET /api/cloud-llm/analysis/{jobId}` for its
status and result. The gateway publishes jobs to JetStream; the cloud worker
runs the Azure-backed agents and publishes results back to JetStream.

`docker compose down` preserves database and JetStream data volumes. Avoid
`docker compose down -v` unless you intend to delete that data.

## Frontend Development

For Vite hot reload, run the frontend outside Docker:

```powershell
cd apps/frontend
npm ci
npm run dev
```

This serves Vite on `http://localhost:5173`. The Compose frontend profile is a
production-style build served by Nginx; use either it or Vite on the same host
port at a time.

Frontend checks, from `apps/frontend`:

```powershell
npm run build
npm run test:unit -- --run
```

## Local LLM

The `local-llm` profile uses the existing vLLM image and local model files; it
does not download model weights and does not create a separate API container.
Set `LLM_MODEL_PATH` and `LLM_MODEL_NAME` in the root `.env`. The model path names a directory under
`apps/localLLM/models`, for example `Qwen2.5-Coder-1.5B-Instruct-AWQ`. That
directory must contain the complete Hugging Face model repository required by
vLLM. Adjust the `VLLM_*` settings in `.env` for your GPU and workload.

## Test And Validate Containers

Build the .NET solution, including the API gateway and worker:

```powershell
dotnet build apps/apiGateway/api.slnx
```

Validate the root Compose file and build all application images (the vLLM image
is pulled separately and its model weights remain on disk):

```powershell
docker compose config --quiet
docker compose --profile frontend --profile local-llm build
```

The `frontend` profile builds the static SPA image. For interactive frontend
development, use `npm run dev` as described above. The local model proxy routes
are hosted by the API gateway in both cases.