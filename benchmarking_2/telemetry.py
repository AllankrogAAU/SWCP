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
    llm_queue_ms: float = 0.0
    llm_exec_ms: float = 0.0
    orchestration_ms: float = 0.0


class LokiTelemetry:
    def __init__(self, url: str | None) -> None:
        self.url = url.rstrip("/") if url else None

    def collect(self, submission_id: str, start: float, end: float) -> Timings:
        timings = Timings()
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
            for _, line in stream.get("values", []):
                start_json = line.find("{")
                if start_json < 0:
                    continue
                try:
                    event = json.loads(line[start_json:])
                except json.JSONDecodeError:
                    continue
                stage = event.get("stage")
                event_name = event.get("event_name")
                duration = float(event.get("duration_ms") or 0)
                if event_name == "pipeline.completed":
                    timings.pipeline_total_ms = duration
                elif event_name == "pipeline.stage.started" and stage == "sandbox":
                    timings.sandbox_queue_ms = float(event.get("queue_wait_ms") or 0)
                elif event_name == "pipeline.stage.completed" and stage == "sandbox":
                    timings.sandbox_exec_ms = duration
                elif event_name == "pipeline.stage.started" and stage == "llm":
                    timings.llm_queue_ms = float(event.get("queue_wait_ms") or 0)
                elif event_name == "pipeline.stage.completed" and stage == "llm":
                    timings.llm_exec_ms = duration
        known = (
            timings.sandbox_queue_ms + timings.sandbox_exec_ms +
            timings.llm_queue_ms + timings.llm_exec_ms
        )
        timings.orchestration_ms = max(0.0, timings.pipeline_total_ms - known)
        return timings
