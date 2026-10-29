import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import nats
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import ValidationError

from contracts import CompilationResult, SandboxResult, SandboxTask, SandboxTestResult
from evaluation.evaluator import evaluate
from sandbox.interface import Limits
from sandbox.k8s_pod import KubeSandboxProvider, PodConfig

NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
TASK_STREAM = "SANDBOX_TASKS"
TASK_SUBJECT = "sandbox.tasks"
TASK_DURABLE = "c-sandbox-workers"
RESULT_SUBJECT = "sandbox.results"
MAX_CONCURRENT = int(os.getenv("SANDBOX_MAX_CONCURRENT", "8"))
POOL_SIZE = int(os.getenv("SANDBOX_POOL_SIZE", "4"))
MAX_ACK_PENDING = int(os.getenv("SANDBOX_MAX_ACK_PENDING", "512"))
HEALTH_FILE = os.getenv("SANDBOX_HEALTH_FILE", "/tmp/healthy")


def touch_health_file() -> None:
    try:
        with open(HEALTH_FILE, "w") as handle:
            handle.write(str(time.time()))
    except OSError:
        logger.exception("Could not write health heartbeat to %s", HEALTH_FILE)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        trace_parent = getattr(record, "traceparent", None)
        trace_parts = trace_parent.split("-") if trace_parent else []
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": "c-sandbox-worker",
            "pod_name": os.getenv("HOSTNAME", socket.gethostname()),
            "trace_id": trace_parts[1] if len(trace_parts) == 4 else None,
            "span_id": trace_parts[2] if len(trace_parts) == 4 else None,
            "submission_id": getattr(record, "submission_id", None),
            "event_name": getattr(record, "event_name", None),
            "stage": getattr(record, "stage", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "queue_wait_ms": getattr(record, "queue_wait_ms", None),
            "attempt": getattr(record, "attempt", None),
            "status": getattr(record, "status", None),
            "outcome": getattr(record, "outcome", None),
            "error_type": getattr(record, "error_type", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), force=True)
for handler in logging.getLogger().handlers:
    handler.setFormatter(JsonLogFormatter())
logger = logging.getLogger("c-sandbox-worker")

OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
if OTEL_ENDPOINT:
    grpc_endpoint = OTEL_ENDPOINT.replace("http://", "").replace("https://", "")
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "c-sandbox-worker")})
    tracer_provider = TracerProvider(sampler=ALWAYS_ON, resource=resource)
    exporter = OTLPSpanExporter(endpoint=grpc_endpoint, insecure=True)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(exporter, schedule_delay_millis=500, max_export_batch_size=32)
    )
    trace.set_tracer_provider(tracer_provider)
else:
    tracer_provider = TracerProvider(sampler=ALWAYS_ON)
    trace.set_tracer_provider(tracer_provider)

tracer = trace.get_tracer("c-sandbox-worker")


def evaluate_task(task: SandboxTask, provider, ctx=None) -> SandboxResult:
    token = context.attach(ctx) if ctx else None
    try:
        with tracer.start_as_current_span("sandbox.execute_code"):
            result = evaluate(
                task.source,
                provider,
                Limits(),
                task.testCases,
                task.harnessSource,
                task.runStdin,
            )
            status = result.status.value
            compiled = status not in {"compile_error", "compile_timeout"}

            with tracer.start_as_current_span("sandbox.compile") as compile_span:
                compile_span.set_attribute("compile.duration_ms", result.compile_ms or 0)
                compile_span.set_attribute("compile.success", compiled)
                if not compiled and result.compile_stderr:
                    compile_span.set_attribute("compile.error", result.compile_stderr[:500])

            test_results: list[SandboxTestResult] = []
            if compiled:
                if task.testCases:
                    for index, (test_case, run) in enumerate(
                        zip(task.testCases, result.test_runs, strict=True)
                    ):
                        passed = run.rc == 0 and run.stdout == test_case.expectedStdout
                        with tracer.start_as_current_span("sandbox.run_test") as test_span:
                            test_span.set_attribute("test.index", index)
                            test_span.set_attribute("run.duration_ms", run.duration_ms)
                            test_span.set_attribute("run.exit_code", run.rc)
                            test_span.set_attribute("run.passed", passed)
                        test_results.append(
                            SandboxTestResult(
                                passed=passed,
                                stdout=run.stdout,
                                stderr=run.stderr,
                                exitCode=run.rc,
                                durationMilliseconds=run.duration_ms,
                                memoryConsumedMegabytes=None,
                            )
                        )
                else:
                    passed = status == "success" and result.exit_code == 0
                    with tracer.start_as_current_span("sandbox.run_binary") as exec_span:
                        exec_span.set_attribute("run.duration_ms", result.run_ms or 0)
                        exec_span.set_attribute("run.exit_code", result.exit_code or 0)
                        exec_span.set_attribute("run.passed", passed)
                    test_results.append(
                        SandboxTestResult(
                            passed=passed,
                            stdout=result.stdout or "",
                            stderr=result.stderr or "",
                            exitCode=result.exit_code,
                            durationMilliseconds=result.run_ms,
                            memoryConsumedMegabytes=None,
                        )
                    )

            limitations = []
            if task.compilerFlags:
                limitations.append("compiler-flags-not-yet-applied")

            return SandboxResult(
                submissionId=task.submissionId,
                compilation=CompilationResult(
                    success=compiled,
                    stdout=result.compile_stdout or "",
                    stderr=result.compile_stderr or "",
                    exitCode=None if compiled else 1,
                    durationMilliseconds=result.compile_ms,
                ),
                staticAnalysis=[],
                testResults=test_results,
                errorClassification=";".join([status, *limitations]),
            )
    finally:
        if token:
            context.detach(token)


async def handle_message(message, connection, jetstream, provider, executor) -> None:
    try:
        task = SandboxTask.model_validate_json(message.data)
    except ValidationError as exception:
        logger.error("Invalid sandbox task; terminating message: %s", exception)
        await jetstream.publish(
            "sandbox.tasks.dlq",
            message.data,
            headers={"Nats-Msg-Id": f"sandbox-poison-{uuid4()}"},
        )
        await message.ack()
        return

    trace_parent = task.traceParent or (message.headers or {}).get("traceparent")
    parent_ctx = None
    if trace_parent:
        parent_ctx = TraceContextTextMapPropagator().extract(carrier={"traceparent": trace_parent})

    attempt = getattr(getattr(message, "metadata", None), "num_delivered", 1)
    stage_started = time.perf_counter()

    try:
        with tracer.start_as_current_span("sandbox.evaluate", context=parent_ctx) as eval_span:
            eval_span.set_attribute("submission.id", str(task.submissionId))
            eval_span.set_attribute("attempt", attempt)

            logger.info(
                "Sandbox evaluation started",
                extra={
                    "event_name": "pipeline.stage.started",
                    "stage": "sandbox",
                    "submission_id": str(task.submissionId),
                    "traceparent": trace_parent,
                    "attempt": attempt,
                    "status": "processing",
                },
            )
            start_headers = {"traceparent": trace_parent} if trace_parent else None
            await connection.publish(
                "submissions.work.started",
                json.dumps(
                    {
                        "submissionId": str(task.submissionId),
                        "worker": "c-sandbox-worker",
                        "stage": "sandbox",
                        "timestampUtc": datetime.now(UTC).isoformat(),
                    }
                ).encode("utf-8"),
                headers=start_headers,
            )
            curr_ctx = context.get_current()
            result = await asyncio.get_running_loop().run_in_executor(
                executor, evaluate_task, task, provider, curr_ctx
            )
            duration_ms = round((time.perf_counter() - stage_started) * 1000)
            eval_span.set_attribute("duration_ms", duration_ms)
            eval_span.set_attribute("outcome", result.errorClassification or "unknown")

            result_headers = {"Nats-Msg-Id": f"{task.submissionId}-sandbox-result"}
            if trace_parent:
                result_headers["traceparent"] = trace_parent
            await jetstream.publish(
                RESULT_SUBJECT,
                result.model_dump_json().encode("utf-8"),
                headers=result_headers,
            )
            await message.ack()
            logger.info(
                "Sandbox evaluation completed",
                extra={
                    "event_name": "pipeline.stage.completed",
                    "stage": "sandbox",
                    "submission_id": str(task.submissionId),
                    "traceparent": trace_parent,
                    "duration_ms": duration_ms,
                    "attempt": attempt,
                    "status": "completed",
                    "outcome": result.errorClassification,
                },
            )
    except Exception:
        delivered = getattr(getattr(message, "metadata", None), "num_delivered", 1)
        if delivered >= 3:
            error_text = "sandbox worker retries exhausted"
            logger.exception(
                "Sandbox evaluation retries exhausted",
                extra={
                    "event_name": "pipeline.stage.failed",
                    "stage": "sandbox",
                    "submission_id": str(task.submissionId),
                    "traceparent": trace_parent,
                    "duration_ms": round((time.perf_counter() - stage_started) * 1000),
                    "attempt": delivered,
                    "status": "failed",
                    "error_type": "RetriesExhausted",
                },
            )
            await jetstream.publish(
                "sandbox.tasks.dlq",
                message.data,
                headers={"Nats-Msg-Id": f"{task.submissionId}-sandbox-dlq"},
            )
            failed_result = SandboxResult(
                submissionId=task.submissionId,
                compilation=CompilationResult(
                    success=False,
                    stdout="",
                    stderr=error_text,
                    exitCode=None,
                    durationMilliseconds=None,
                ),
                staticAnalysis=[],
                testResults=[],
                errorClassification=f"worker-error:{error_text}",
            )
            await jetstream.publish(
                RESULT_SUBJECT,
                failed_result.model_dump_json().encode("utf-8"),
                headers={"Nats-Msg-Id": f"{task.submissionId}-sandbox-result"},
            )
            await message.ack()
            logger.error(
                "Sandbox retries exhausted for submission %s; sent to DLQ",
                task.submissionId,
            )
            return

        delay = min(30.0, 2.0 ** min(delivered, 5)) + random.random()
        logger.exception(
            "Sandbox task failed; retrying after %.1f seconds",
            delay,
            extra={
                "event_name": "pipeline.stage.retrying",
                "stage": "sandbox",
                "submission_id": str(task.submissionId),
                "traceparent": trace_parent,
                "duration_ms": round((time.perf_counter() - stage_started) * 1000),
                "attempt": delivered,
                "status": "retrying",
            },
        )
        await asyncio.sleep(delay)
        await message.nak()
    finally:
        tracer_provider.force_flush()


async def run_worker() -> None:
    connection = await nats.connect(NATS_URL, name="c-sandbox-worker")
    jetstream = connection.jetstream()
    consumer_config = ConsumerConfig(
        durable_name=TASK_DURABLE,
        ack_policy=AckPolicy.EXPLICIT,
        filter_subject=TASK_SUBJECT,
        ack_wait=120,
        max_deliver=3,
        max_ack_pending=MAX_ACK_PENDING,
    )
    try:
        await jetstream.add_consumer(TASK_STREAM, consumer_config)
    except Exception:
        logger.exception(
            "Could not create/update consumer %s; binding to existing", TASK_DURABLE
        )
    consumer = await jetstream.pull_subscribe_bind(TASK_DURABLE, stream=TASK_STREAM)

    provider = KubeSandboxProvider(PodConfig.from_env(), pool_size=POOL_SIZE)
    executor = ThreadPoolExecutor(
        max_workers=MAX_CONCURRENT, thread_name_prefix="sandbox"
    )
    slots = asyncio.Semaphore(MAX_CONCURRENT)
    running: set[asyncio.Task] = set()
    logger.info(
        "Consuming %s (max_concurrent=%d, pool_size=%d)",
        TASK_SUBJECT,
        MAX_CONCURRENT,
        POOL_SIZE,
    )

    async def process(message) -> None:
        try:
            await handle_message(message, connection, jetstream, provider, executor)
        except Exception:
            logger.exception("Unhandled error while processing sandbox message")
        finally:
            slots.release()

    async def heartbeat_loop(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            touch_health_file()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=1)

    stop_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(heartbeat_loop(stop_heartbeat))

    # Without an explicit handler, SIGTERM (sent by Kubernetes on pod
    # termination/rollout) kills the process immediately via the OS default
    # action, skipping the `finally` block below -- which otherwise orphans
    # this worker's warm-pool sandbox pods until the janitor's 30-minute
    # max-age sweep. Translate it into a cooperative shutdown instead.
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.set)

    try:
        while not shutdown.is_set():
            try:
                await asyncio.wait_for(slots.acquire(), timeout=1)
            except TimeoutError:
                continue
            try:
                messages = await consumer.fetch(batch=1, timeout=1)
            except NatsTimeoutError:
                slots.release()
                continue
            except BaseException:
                slots.release()
                raise
            task = asyncio.create_task(process(messages[0]))
            running.add(task)
            task.add_done_callback(running.discard)
    finally:
        stop_heartbeat.set()
        await heartbeat_task
        if running:
            await asyncio.wait(running, timeout=30)
        executor.shutdown(wait=False, cancel_futures=True)
        provider.close()
        await connection.drain()
        await connection.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
