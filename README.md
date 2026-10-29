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
4. The trusted sandbox worker compiles and lints the submission once, then
	runs it once for each teacher-provided test case, passing the case's stdin
	and applying its timeout and memory limits. It publishes the per-case
	results to `sandbox.results`. Assignments without test cases keep the
	single-run behavior.
5. Core stores compiler/runtime output. Compile failures complete immediately;
	successful compilation builds a prompt and publishes `llm.tasks.azure` or
	`llm.tasks.local`.
6. `llmWorker` calls Azure AI Foundry or the OpenAI-compatible local vLLM API,
	publishes `llm.results`, and Core stores final feedback.
7. The frontend receives `notifications.<submissionId>` over WebSocket and also
	polls `GET /api/submissions/{id}` as a fallback.

Assignment test cases are returned by the assignment API so students can use
them to understand and check the expected behavior. A test passes only when
the program exits successfully and its stdout exactly matches the expected
stdout. Compiler flags are accepted by the worker contract but are not yet
applied.

Teachers can create assignments with a `testCases` array using
`POST /api/assignments/`. To replace the tests on an existing assignment, send
`PUT /api/assignments/{assignmentId}/test-cases` with a teacher access token:

```json
{
  "testCases": [
    {
      "stdin": "2 3\n",
      "expectedStdout": "5\n",
      "timeoutMilliseconds": 2000,
      "memoryLimitMegabytes": 64
    }
  ]
}
```

The endpoint replaces the assignment's full test-case list; pass an empty array
to clear it. Test cases are stored with their assignment in PostgreSQL's
`Assignments.TestCasesJson` JSONB column and are used for subsequent submissions.

For function-based assignments where the student's C source does not define
`main`, teachers can provide trusted C harness source in the optional
`harnessSource` field when creating an assignment. The harness declares and
calls the required student function and provides `main`; Core compiles it with
the student's source. To set or replace it later, use the teacher-only
`PUT /api/assignments/{assignmentId}/harness` endpoint with
`{"harnessSource":"...C source..."}`. Send `null` or an empty string to remove
the harness. Harness source is stored in the assignment row and limited to
64 KB UTF-8. Teachers can retrieve it with
`GET /api/assignments/{assignmentId}/harness`; it is not included in the
student-facing assignment response.

For example, students can submit `int add(int a, int b) { return a + b; }`,
while the teacher's harness declares `int add(int, int);` and defines `main`
to read two integers, call `add`, and print its result. Saved test cases supply
the harness input and compare its output as usual.

The frontend Run action accepts standard input, which is passed to the program
(or to the harness `main`) for one execution. Submit and Hint continue to run
the assignment's saved test cases independently.

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



.\scripts\Show-PipelineTimings.ps1
