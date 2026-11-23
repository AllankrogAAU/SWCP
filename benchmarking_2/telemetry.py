from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass


@dataclass
class Timings:
    pipeline_total_ms: float = 0.0
    sandbox_queue_ms: float = 0.0
    sandbox_exec_ms: float = 0.0
    sandbox_acquire_ms: float = 0.0
    sandbox_acquire_source: str = "unknown"
    sandbox_compile_ms: float = 0.0
    sandbox_run_ms: float = 0.0
    sandbox_backend: str = ""
    sandbox_runtime: str = ""
    sandbox_worker_node: str = ""
    sandbox_worker_pod: str = ""
    llm_queue_ms: float = 0.0
    llm_exec_ms: float = 0.0
    orchestration_ms: float = 0.0


class LokiTelemetry:
    def __init__(self, url: str | None) -> None:
        self.url = url.rstrip("/") if url else None

    def collect(self, submission_id: str, start: float, end: float) -> Timings:
        timings = Timings()
        if not self.url or not submission_id:
            return timings
        for attempt in range(3):
            timings = self._query_once(submission_id, start, max(end, time.time()))
            if timings.pipeline_total_ms > 0 and timings.llm_exec_ms > 0:
                break
            if attempt < 2:
                time.sleep(0.5)
        return timings

    def _query_once(self, submission_id: str, start: float, end: float) -> Timings:
        timings = Timings()
        submission_enqueue_ns = 0
        sandbox_completed_ns = 0
        if not self.url:
            return timings
        params = urllib.parse.urlencode({
            "query": f'{{namespace="swcp"}} |= "{submission_id}" |= "pipeline."',
            "start": str(int((start - 10) * 1_000_000_000)),
            "end": str(int((end + 15) * 1_000_000_000)),
            "limit": "500",
            "direction": "FORWARD",
        })
        try:
            with urllib.request.urlopen(f"{self.url}/loki/api/v1/query_range?{params}", timeout=15) as response:
                data = json.loads(response.read())
        except (OSError, ValueError):
            return timings
        for stream in data.get("data", {}).get("result", []):
            stream_labels = stream.get("stream", {})
            stream_node = str(stream_labels.get("node", "") or "")
            stream_pod = str(stream_labels.get("pod", "") or "")
            for timestamp_ns, line in stream.get("values", []):
                timestamp_ns = int(timestamp_ns)
                start_json = line.find("{")
                if start_json < 0:
                    continue
                try:
                    payload = json.loads(line[start_json:])
                except json.JSONDecodeError:
                    continue
                state = payload.get("State") or payload.get("Properties") or payload
                if not isinstance(state, dict):
                    state = payload
                stage = state.get("stage") or payload.get("stage")
                event_name = state.get("event_name") or payload.get("event_name")
                duration = float(state.get("duration_ms") or payload.get("duration_ms") or 0)
                queue_wait = state.get("queue_wait_ms")
                if queue_wait is None:
                    queue_wait = payload.get("queue_wait_ms")
                if event_name == "pipeline.completed":
                    timings.pipeline_total_ms = duration
                elif event_name == "pipeline.stage.completed" and stage == "submission_enqueue":
                    submission_enqueue_ns = timestamp_ns
                elif event_name == "pipeline.stage.started" and stage == "sandbox":
                    timings.sandbox_queue_ms = (
                        float(queue_wait or 0)
                        if queue_wait is not None
                        else (
                            max(0.0, (timestamp_ns - submission_enqueue_ns) / 1_000_000)
                            if submission_enqueue_ns
                            else 0.0
                        )
                    )
                elif event_name == "pipeline.stage.completed" and stage == "sandbox":
                    timings.sandbox_exec_ms = duration
                    sandbox_completed_ns = timestamp_ns
                    timings.sandbox_worker_node = stream_node
                    timings.sandbox_worker_pod = (
                        stream_pod
                        or str(state.get("pod_name", "") or payload.get("pod_name", "") or "")
                    )
                    timings.sandbox_backend = str(
                        state.get("sandbox_backend", "") or payload.get("sandbox_backend", "") or ""
                    )
                    timings.sandbox_runtime = str(
                        state.get("sandbox_runtime", "") or payload.get("sandbox_runtime", "") or ""
                    )
                    timings.sandbox_acquire_ms = float(
                        state.get("sandbox_acquire_ms") or payload.get("sandbox_acquire_ms") or 0
                    )
                    timings.sandbox_acquire_source = str(
                        state.get("sandbox_acquire_source", "")
                        or payload.get("sandbox_acquire_source", "")
                        or "unknown"
                    )
                    timings.sandbox_compile_ms = float(
                        state.get("sandbox_compile_ms") or payload.get("sandbox_compile_ms") or 0
                    )
                    timings.sandbox_run_ms = float(
                        state.get("sandbox_run_ms") or payload.get("sandbox_run_ms") or 0
                    )
                elif event_name == "pipeline.stage.started" and stage == "llm":
                    timings.llm_queue_ms = (
                        float(queue_wait or 0)
                        if queue_wait is not None
                        else (
                            max(0.0, (timestamp_ns - sandbox_completed_ns) / 1_000_000)
                            if sandbox_completed_ns
                            else 0.0
                        )
                    )
                elif event_name == "pipeline.stage.completed" and stage == "llm":
                    timings.llm_exec_ms = duration
        known = (
            timings.sandbox_queue_ms + timings.sandbox_exec_ms +
            timings.llm_queue_ms + timings.llm_exec_ms
        )
        timings.orchestration_ms = max(0.0, timings.pipeline_total_ms - known)
        return timings
