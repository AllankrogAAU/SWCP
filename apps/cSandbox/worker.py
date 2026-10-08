import asyncio
import json
import logging
import os
import random
import socket
from datetime import UTC, datetime
from uuid import uuid4

import nats
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig
from pydantic import ValidationError

from contracts import CompilationResult, SandboxResult, SandboxTask, SandboxTestResult
from evaluation.evaluator import evaluate

NATS_URL = os.getenv("NATS_URL", "nats://localhost:4222")
TASK_STREAM = "SANDBOX_TASKS"
TASK_SUBJECT = "sandbox.tasks"
TASK_DURABLE = "c-sandbox-workers"
RESULT_SUBJECT = "sandbox.results"

class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        trace_parent = getattr(record, "traceparent", None)
        trace_parts = trace_parent.split("-") if trace_parent else []
        return json.dumps({
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": "c-sandbox-worker",
            "pod_name": os.getenv("HOSTNAME", socket.gethostname()),
            "trace_id": trace_parts[1] if len(trace_parts) == 4 else None,
            "span_id": trace_parts[2] if len(trace_parts) == 4 else None,
            "submission_id": getattr(record, "submission_id", None),
            "message": record.getMessage(),
        }, default=str)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), force=True)
for handler in logging.getLogger().handlers:
    handler.setFormatter(JsonLogFormatter())
logger = logging.getLogger("c-sandbox-worker")


def evaluate_task(task: SandboxTask) -> SandboxResult:
    result = evaluate(task.source)
    status = result.status.value
    compiled = status not in {"compile_error", "compile_timeout"}
    test_results = []

    if compiled:
        test_results.append(
            SandboxTestResult(
                passed=status == "success" and result.exit_code == 0,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                exitCode=result.exit_code,
                durationMilliseconds=None,
                memoryConsumedMegabytes=None,
            )
        )

    limitations = []
    if task.compilerFlags:
        limitations.append("compiler-flags-not-yet-applied")
    if task.testCases:
        limitations.append("per-test-case-execution-not-yet-implemented")

    return SandboxResult(
        submissionId=task.submissionId,
        compilation=CompilationResult(
            success=compiled,
            stdout=result.compile_stdout or "",
            stderr=result.compile_stderr or "",
            exitCode=None if compiled else 1,
            durationMilliseconds=None,
        ),
        staticAnalysis=[],
        testResults=test_results,
        errorClassification=";".join([status, *limitations]),
    )


async def handle_message(message, connection, jetstream) -> None:
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

    try:
        trace_parent = task.traceParent or (message.headers or {}).get("traceparent")
        start_headers = {"traceparent": trace_parent} if trace_parent else None
        await connection.publish(
            "submissions.work.started",
            json.dumps({
                "submissionId": str(task.submissionId),
                "worker": "c-sandbox-worker",
                "stage": "sandbox",
                "timestampUtc": datetime.now(UTC).isoformat(),
            }).encode("utf-8"),
            headers=start_headers,
        )
        result = await asyncio.to_thread(evaluate_task, task)
        result_headers = {"Nats-Msg-Id": f"{task.submissionId}-sandbox-result"}
        if trace_parent:
            result_headers["traceparent"] = trace_parent
        await jetstream.publish(
            RESULT_SUBJECT,
            result.model_dump_json().encode("utf-8"),
            headers=result_headers,
        )
        await message.ack()
        logger.info("Published sandbox result", extra={"submission_id": str(task.submissionId), "traceparent": trace_parent})
    except Exception:
        delivered = getattr(getattr(message, "metadata", None), "num_delivered", 1)
        if delivered >= 3:
            error_text = "sandbox worker retries exhausted"
            await jetstream.publish(
                "sandbox.tasks.dlq",
                message.data,
                headers={"Nats-Msg-Id": f"{task.submissionId}-sandbox-dlq"},
            )
            failed_result = SandboxResult(
                submissionId=task.submissionId,
                compilation=CompilationResult(False, "", error_text, None, None),
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
            logger.error("Sandbox retries exhausted for submission %s; sent to DLQ", task.submissionId)
            return

        delay = min(30.0, 2.0 ** min(delivered, 5)) + random.random()
        logger.exception("Sandbox task failed; retrying after %.1f seconds", delay)
        await message.nak(delay=delay)


async def run_worker() -> None:
    connection = await nats.connect(NATS_URL, name="c-sandbox-worker")
    jetstream = connection.jetstream()
    consumer = await jetstream.pull_subscribe(
        TASK_SUBJECT,
        durable=TASK_DURABLE,
        stream=TASK_STREAM,
        config=ConsumerConfig(
            durable_name=TASK_DURABLE,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=60,
            max_deliver=3,
            max_ack_pending=4,
        ),
    )
    logger.info("Consuming JetStream subject %s", TASK_SUBJECT)

    try:
        while True:
            try:
                messages = await consumer.fetch(batch=1, timeout=1)
            except NatsTimeoutError:
                continue
            for message in messages:
                await handle_message(message, connection, jetstream)
    finally:
        await connection.drain()
        await connection.close()


if __name__ == "__main__":
    asyncio.run(run_worker())