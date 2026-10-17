# Messaging Contracts

All message bodies are UTF-8 JSON. W3C `traceparent` is carried in the NATS
message header; message identifiers use `Nats-Msg-Id` for JetStream publish
deduplication.

| Stream | Subjects | Retention | Consumer policy |
| --- | --- | --- | --- |
| `SANDBOX_TASKS` | `sandbox.tasks` | WorkQueue | Durable `c-sandbox-workers`; explicit ack; max delivery 3 |
| `SANDBOX_RESULTS` | `sandbox.results` | WorkQueue | Durable `core-api-sandbox-results`; explicit ack |
| `LLM_TASKS` | `llm.tasks.azure`, `llm.tasks.local` | WorkQueue | Separate durable consumers; explicit ack; max delivery 3 |
| `LLM_RESULTS` | `llm.results` | WorkQueue | Durable `core-api-llm-results`; explicit ack |
| `SANDBOX_TASKS_DLQ` | `sandbox.tasks.dlq` | Limits | Sandbox worker copies malformed/exhausted tasks |
| `LLM_TASKS_DLQ` | `llm.tasks.dlq` | Limits | LLM worker copies exhausted retryable tasks |

The Core API publishes tasks with `Nats-Msg-Id` set to
`<submission-id>-sandbox-<attempt>` or `<submission-id>-llm`. The task body and
NATS header carry W3C `traceparent`. Workers publish `submissions.work.started`
over Core NATS for persisted processing-state transitions. Notifications use
Core NATS subjects `notifications.<submission-id>` and are transient progress
events, not the source of truth for submission state.

Consumer starting values from the architecture plan: local LLM maximum pending
4 and 60-second ack wait; Azure maximum pending 50 and 30-second ack wait. Tune
these against model latency and GPU capacity before production use.