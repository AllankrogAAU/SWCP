from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import websockets

from .config import Config, ModelSpec
from .dataset import Submission
from .telemetry import LokiTelemetry

logger = logging.getLogger(__name__)


@dataclass
class Result:
    item: Submission
    model: ModelSpec
    submission_id: str = ""
    status: str = "FAILED"
    feedback: str | None = None
    sandbox_output: str | None = None
    error: str | None = None
    client_e2e_ms: float = 0.0
    http_accepted_ms: float = 0.0
    timings: Any = None


class PipelineClient:
    def __init__(self, config: Config, model: ModelSpec, tokens: list[str], assignments: dict[str, str]) -> None:
        self.config, self.model, self.tokens, self.assignments = config, model, tokens, assignments
        self.telemetry = LokiTelemetry(config.loki_url)
        self.index = 0

    def _token(self) -> str:
        token = self.tokens[self.index % len(self.tokens)]
        self.index += 1
        return token

    def _ws_url(self, submission_id: str) -> str:
        parsed = urlparse(self.config.target_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc or 'localhost'}/ws/submissions/{submission_id}"

    async def submit(self, item: Submission, warmup: bool = False) -> Result:
        token = self._token()
        result = Result(item=item, model=self.model, timings=None)
        started = time.perf_counter()
        started_epoch = time.time()
        assignment_detail = self.assignments.get(item.assignment_title)
        assignment_id = (
            assignment_detail.get("id")
            if isinstance(assignment_detail, dict)
            else assignment_detail
        )
        if not assignment_id:
            result.error = f"No API assignment matches {item.assignment_title!r}."
            return result
        payload = {
            "assignmentId": assignment_id, "sourceCode": item.source_code,
            "standardInput": item.standard_input, "llmBackend": self.model.backend, "action": "submit",
        }
        try:
            async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30) as client:
                response = await client.post("/api/submissions", json=payload, headers={"Authorization": f"Bearer {token}"})
                result.http_accepted_ms = (time.perf_counter() - started) * 1000
                response.raise_for_status()
                body = response.json()
                result.submission_id = str(body.get("submissionId") or body.get("id"))
                result.status = await self._wait(result.submission_id, token, client)
                details = await client.get(
                    f"/api/submissions/{result.submission_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if details.is_success:
                    data = details.json()
                    result.feedback = data.get("llmFeedback")
                    result.sandbox_output = json.dumps(data.get("sandboxOutput")) if data.get("sandboxOutput") else None
                result.client_e2e_ms = (time.perf_counter() - started) * 1000
                result.timings = await asyncio.to_thread(
                    self.telemetry.collect, result.submission_id, started_epoch, time.time()
                )
        except Exception as exc:
            result.error = str(exc)
            result.client_e2e_ms = (time.perf_counter() - started) * 1000
            logger.error("Submission %s failed: %s", item.record_id, exc)
        return result

    async def _wait(self, submission_id: str, token: str, client: httpx.AsyncClient) -> str:
        async def websocket_wait() -> str | None:
            try:
                options = {
                    "subprotocols": ["swcp", f"bearer.{token}"],
                    "open_timeout": 10,
                }
                try:
                    socket_context = websockets.connect(
                        self._ws_url(submission_id),
                        additional_headers={"Authorization": f"Bearer {token}"},
                        **options,
                    )
                except TypeError:
                    socket_context = websockets.connect(
                        self._ws_url(submission_id),
                        extra_headers={"Authorization": f"Bearer {token}"},
                        **options,
                    )
                async with socket_context as socket:
                    async for raw in socket:
                        event = json.loads(raw)
                        if str(event.get("eventType", "")).upper() in {"COMPLETED", "FAILED"}:
                            return str(event["eventType"]).upper()
            except Exception:
                return None
            return None

        ws_task = asyncio.create_task(websocket_wait())
        try:
            deadline = time.monotonic() + self.config.timeout_s
            while time.monotonic() < deadline:
                response = await client.get(
                    f"/api/submissions/{submission_id}", headers={"Authorization": f"Bearer {token}"}
                )
                if response.is_success:
                    status = str(response.json().get("status", "")).upper()
                    if status in {"COMPLETED", "FAILED"}:
                        return status
                await asyncio.sleep(1)
            return "TIMEOUT"
        finally:
            ws_task.cancel()
            await asyncio.gather(ws_task, return_exceptions=True)
