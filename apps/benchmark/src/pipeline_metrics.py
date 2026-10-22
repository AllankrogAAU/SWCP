"""Read structured pipeline events from Docker Compose logs."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any


def parse_pipeline_logs(text: str) -> dict[str, list[dict[str, Any]]]:
    """Group structured pipeline events by submission ID."""
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            outer = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        state = outer.get("State") or outer
        event_name = state.get("event_name")
        submission_id = state.get("submission_id")
        if not submission_id or not isinstance(event_name, str) or not event_name.startswith("pipeline."):
            continue
        timestamp = outer.get("Timestamp") or outer.get("timestamp") or state.get("timestamp")
        events[str(submission_id)].append({
            "event_name": event_name,
            "stage": state.get("stage"),
            "timestamp": timestamp,
            "duration_ms": state.get("duration_ms"),
            "queue_wait_ms": state.get("queue_wait_ms"),
            "queue_publish_ms": state.get("queue_publish_ms"),
            "prompt_tokens": state.get("prompt_tokens"),
            "completion_tokens": state.get("completion_tokens"),
            "status": state.get("status"),
        })
    return dict(events)


def summarize_submission(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the latest completed stage measurements for one submission."""
    stages: dict[str, dict[str, Any]] = {}
    for event in events:
        stage = event.get("stage")
        if event["event_name"] == "pipeline.stage.completed" and stage:
            stages[str(stage)] = event

    started: dict[str, dict[str, Any]] = {}
    for event in events:
        stage = event.get("stage")
        if event["event_name"] == "pipeline.stage.started" and stage:
            started[str(stage)] = event

    completed = stages.get("llm", {})
    total = next(
        (event for event in reversed(events) if event["event_name"] == "pipeline.completed"),
        {},
    )
    enqueue = next(
        (
            event for event in events
            if event["event_name"] == "pipeline.stage.completed"
            and event.get("stage") == "submission_enqueue"
        ),
        {},
    )
    return {
        "pipeline_breakdown_ms": {
            "submission_enqueue": _number(enqueue.get("duration_ms")),
            "nats_task_publish": _number(enqueue.get("queue_publish_ms")),
            "sandbox_queue": _number(started.get("sandbox", {}).get("queue_wait_ms")),
            "sandbox_execution": _number(stages.get("sandbox", {}).get("duration_ms")),
            "llm_queue": _number(started.get("llm", {}).get("queue_wait_ms")),
            "llm_inference": _number(completed.get("duration_ms")),
            "pipeline_total": _number(total.get("duration_ms")),
        },
        "prompt_tokens": _number(completed.get("prompt_tokens")),
        "completion_tokens": _number(completed.get("completion_tokens")),
    }


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
