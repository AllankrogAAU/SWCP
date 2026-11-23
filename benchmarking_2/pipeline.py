from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
import websockets

from .config import Config, ModelSpec
from .dataset import Submission
from .telemetry import LokiTelemetry

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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
    confidence_score_percent: float | None = None
    timings: Any = None


class PipelineClient:
    def __init__(self, config: Config, model: ModelSpec, tokens: list[str], assignments: dict[str, Any]) -> None:
        self.config, self.model, self.tokens, self.assignments = config, model, list(tokens), assignments
        self.telemetry = LokiTelemetry(config.loki_url)
        self.index = 0
        self.current_token_slot = 0
        self._token_queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        for slot, token in enumerate(self.tokens):
            self._token_queue.put_nowait((slot, token))

    async def _acquire_token(self) -> tuple[int, str]:
        if not self._token_queue.empty():
            return self._token_queue.get_nowait()
        return await self._token_queue.get()

    async def _release_token(self, slot: int, token: str) -> None:
        self.tokens[slot] = token
        await self._token_queue.put((slot, token))

    def _ws_url(self, submission_id: str) -> str:
        parsed = urlparse(self.config.target_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc or 'localhost'}/ws/submissions/{submission_id}"

    async def submit(
        self,
        item: Submission,
        warmup: bool = False,
        shared_client: httpx.AsyncClient | None = None,
    ) -> Result:
        slot, token = await self._acquire_token()
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
            await self._release_token(slot, token)
            return result
        payload = {
            "assignmentId": assignment_id,
            "sourceCode": item.source_code,
            "standardInput": item.standard_input,
            "llmBackend": self.model.backend,
            "action": "submit",
        }
        try:
            if shared_client is not None:
                token = await self._execute_submission(
                    shared_client, payload, slot, token, result, started, started_epoch
                )
            else:
                async with httpx.AsyncClient(
                    base_url=self.config.target_url,
                    timeout=30.0,
                    limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
                ) as client:
                    token = await self._execute_submission(
                        client, payload, slot, token, result, started, started_epoch
                    )
        except Exception as exc:
            result.error = str(exc) or type(exc).__name__
            result.client_e2e_ms = (time.perf_counter() - started) * 1000
            logger.error("Submission %s failed: %s", item.record_id, result.error)
        finally:
            await self._release_token(slot, token)
        return result

    async def _execute_submission(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        slot: int,
        token: str,
        result: Result,
        started: float,
        started_epoch: float,
    ) -> str:
        response = await client.post(
            "/api/submissions",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            refreshed = await self._refresh_token(slot, token, client)
            if refreshed:
                token = refreshed
                started_retry = time.perf_counter()
                response = await client.post(
                    "/api/submissions",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                result.http_accepted_ms = (time.perf_counter() - started_retry) * 1000
            else:
                result.http_accepted_ms = (time.perf_counter() - started) * 1000
        else:
            result.http_accepted_ms = (time.perf_counter() - started) * 1000

        response.raise_for_status()
        body = response.json()
        result.submission_id = str(body.get("submissionId") or body.get("id"))
        result.status = await self._wait(result.submission_id, token, client)
        completed_perf = time.perf_counter()
        completed_epoch = time.time()
        details = await client.get(
            f"/api/submissions/{result.submission_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if details.is_success:
            data = details.json()
            result.feedback = data.get("llmFeedback")
            result.sandbox_output = (
                json.dumps(data.get("sandboxOutput"))
                if data.get("sandboxOutput")
                else None
            )
            confidence_score = data.get("confidenceScorePercent")
            if confidence_score is not None:
                if (
                    isinstance(confidence_score, bool)
                    or not isinstance(confidence_score, (int, float))
                    or not math.isfinite(confidence_score)
                    or not 0 <= confidence_score <= 100
                ):
                    raise ValueError(
                        "Submission API returned an invalid confidenceScorePercent."
                    )
                result.confidence_score_percent = float(confidence_score)
            if not result.feedback and str(data.get("status", "")).upper() in {"COMPLETED", "FAILED"}:
                result.status = str(data.get("status", "")).upper()
        result.client_e2e_ms = (completed_perf - started) * 1000
        result.timings = await asyncio.to_thread(
            self.telemetry.collect,
            result.submission_id,
            started_epoch,
            completed_epoch,
        )
        return token

    async def submit_batch(
        self,
        items: list[Submission],
        target_rpm: float,
        max_concurrency: int,
        on_complete: Callable[[Result], None] | None = None,
    ) -> list[Result]:
        if not items:
            return []
        concurrency = max(1, int(max_concurrency))
        interval_s = 60.0 / target_rpm if target_rpm and target_rpm > 0 else 0.0
        semaphore = asyncio.Semaphore(concurrency)
        results_by_index: list[Result | None] = [None] * len(items)
        completed_count = 0
        total = len(items)
        batch_start = time.perf_counter()

        limits = httpx.Limits(
            max_connections=max(64, concurrency * 4),
            max_keepalive_connections=max(32, concurrency * 2),
        )
        async with httpx.AsyncClient(
            base_url=self.config.target_url,
            timeout=30.0,
            limits=limits,
        ) as shared_client:
            async def worker(idx: int, submission_item: Submission) -> None:
                nonlocal completed_count
                try:
                    res = await self.submit(submission_item, warmup=False, shared_client=shared_client)
                    results_by_index[idx] = res
                    if on_complete is not None:
                        on_complete(res)
                    completed_count += 1
                    if completed_count % 25 == 0 or completed_count == total:
                        elapsed_m = max(1e-6, (time.perf_counter() - batch_start) / 60.0)
                        achieved_rpm = completed_count / elapsed_m
                        logger.info(
                            "[%s] Progress: %d/%d completed (%.1f submissions/min, last E2E=%.0fms, status=%s)",
                            self.model.name,
                            completed_count,
                            total,
                            achieved_rpm,
                            res.client_e2e_ms,
                            res.status,
                        )
                finally:
                    semaphore.release()

            tasks: list[asyncio.Task[None]] = []
            next_dispatch = time.perf_counter()
            for idx, submission_item in enumerate(items):
                if interval_s > 0 and idx > 0:
                    now = time.perf_counter()
                    if next_dispatch > now:
                        await asyncio.sleep(next_dispatch - now)
                await semaphore.acquire()
                if interval_s > 0:
                    next_dispatch = max(next_dispatch + interval_s, time.perf_counter())
                tasks.append(asyncio.create_task(worker(idx, submission_item)))

            await asyncio.gather(*tasks)

        return [res for res in results_by_index if res is not None]

    async def _refresh_token(self, slot: int, token: str, client: httpx.AsyncClient) -> str | None:
        try:
            parts = token.split(".")
            padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
            claims = json.loads(base64.urlsafe_b64decode(padded.encode()))
            username = (
                claims.get("preferred_username")
                or claims.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
                or claims.get("unique_name")
            )
            if not username:
                return None
            response = await client.post(
                "/api/auth/login",
                json={"username": username, "password": self.config.password},
            )
            if not response.is_success:
                return None
            refreshed = str(
                response.json().get("accessToken")
                or response.json().get("token")
                or ""
            )
            if refreshed:
                logger.warning("Token expired during run for user %s. Refreshed it.", username)
                self.tokens[slot] = refreshed
                return refreshed
        except Exception as exc:
            logger.warning("Could not refresh expired benchmark token: %s", exc)
        return None

    async def _wait(self, submission_id: str, token: str, client: httpx.AsyncClient) -> str:
        done = asyncio.Event()

        async def sse_wait() -> str | None:
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
            }
            try:
                stream_timeout = httpx.Timeout(self.config.timeout_s, connect=10.0, read=self.config.timeout_s)
                async with client.stream(
                    "GET",
                    f"/api/submissions/{submission_id}/events",
                    headers=headers,
                    timeout=stream_timeout,
                ) as response:
                    if response.status_code != 200:
                        return None
                    async for line in response.aiter_lines():
                        if done.is_set():
                            return None
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        raw_data = line[5:].strip()
                        if not raw_data:
                            continue
                        try:
                            event = json.loads(raw_data)
                        except json.JSONDecodeError:
                            continue
                        status = str(
                            event.get("eventType")
                            or event.get("status")
                            or ""
                        ).upper()
                        if status in {"COMPLETED", "FAILED"}:
                            done.set()
                            return status
            except Exception:
                return None
            return None

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
                        if done.is_set():
                            return None
                        event = json.loads(raw)
                        status = str(event.get("eventType", "")).upper()
                        if status in {"COMPLETED", "FAILED"}:
                            done.set()
                            return status
            except Exception:
                return None
            return None

        async def poll_wait() -> str:
            deadline = time.monotonic() + self.config.timeout_s
            while time.monotonic() < deadline and not done.is_set():
                try:
                    response = await client.get(
                        f"/api/submissions/{submission_id}",
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    if response.is_success:
                        status = str(response.json().get("status", "")).upper()
                        if status in {"COMPLETED", "FAILED"}:
                            done.set()
                            return status
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            return "TIMEOUT"

        sse_task = asyncio.create_task(sse_wait())
        ws_task = asyncio.create_task(websocket_wait())
        poll_task = asyncio.create_task(poll_wait())
        tasks = {sse_task, ws_task, poll_task}
        try:
            while tasks:
                finished, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    try:
                        res = task.result()
                    except Exception:
                        res = None
                    if res in {"COMPLETED", "FAILED", "TIMEOUT"}:
                        return res
                tasks = pending
            return "TIMEOUT"
        finally:
            done.set()
            for task in (sse_task, ws_task, poll_task):
                task.cancel()
            await asyncio.gather(sse_task, ws_task, poll_task, return_exceptions=True)
