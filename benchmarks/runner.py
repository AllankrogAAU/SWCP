"""Asynchronous load generation engine with queue-isolated users and dual WS/polling telemetry."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine
from urllib.parse import urlparse

import httpx
import numpy as np
import websockets

from benchmarks.config import BenchmarkConfig
from benchmarks.workloads import get_workload_source, pick_archetype

logger = logging.getLogger("benchmarks.runner")


@dataclass
class ClientSubmissionResult:
    submission_id: str
    token: str
    step_label: str
    fixture_type: str
    client_e2e_ms: float
    http_accepted_ms: float
    final_status: str
    error_message: str | None = None
    terminal_via: str = "unknown"
    ws_events_count: int = 0
    dispatched_epoch: float = 0.0
    completed_epoch: float = 0.0


class LoadRunner:
    def __init__(
        self,
        config: BenchmarkConfig,
        tokens: list[str],
        assignment_id: str,
        run_id: str,
    ) -> None:
        self.config = config
        self.tokens = tokens
        self.assignment_id = assignment_id
        self.run_id = run_id
        self.user_queue: asyncio.Queue[str] = asyncio.Queue()
        for token in tokens:
            self.user_queue.put_nowait(token)

        self.step_windows: dict[str, tuple[float, float]] = {}
        self.step_meta: dict[str, dict[str, Any]] = {}
        self._submission_seq = 0
        self._seq_lock = asyncio.Lock()
        self._user_wait_events = 0

    async def _next_seq(self) -> int:
        async with self._seq_lock:
            self._submission_seq += 1
            return self._submission_seq

    def _resolve_ws_url(self, submission_id: str) -> str:
        parsed = urlparse(self.config.target_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        netloc = parsed.netloc
        if not netloc:
            netloc = "localhost"
        return f"{scheme}://{netloc}/ws/submissions/{submission_id}"

    async def execute_single_submission(
        self,
        step_label: str,
        client: httpx.AsyncClient,
        archetype: str | None = None,
        timeout_s: float = 240.0,
    ) -> ClientSubmissionResult:
        """Execute one complete submission lifecycle guaranteed to have a dedicated user."""
        if self.user_queue.empty():
            t_wait = time.perf_counter()
            token = await self.user_queue.get()
            waited_ms = (time.perf_counter() - t_wait) * 1000.0
            if waited_ms > 50.0:
                self._user_wait_events += 1
                if self._user_wait_events in (1, 10, 50) or self._user_wait_events % 100 == 0:
                    logger.warning(
                        "Virtual user pool exhausted (%d wait events; latest waited %.0fms) — increase --users to avoid throttling arrival rate!",
                        self._user_wait_events, waited_ms,
                    )
        else:
            token = self.user_queue.get_nowait()
        seq = await self._next_seq()
        archetype = archetype or pick_archetype(self.config.workload_weights)
        c_code = get_workload_source(archetype, self.run_id, seq)

        t0 = time.perf_counter()
        t0_epoch = time.time()
        submission_id = ""
        http_accepted_ms = 0.0

        try:
            # 1. Post submission to API
            payload = {
                "assignmentId": self.assignment_id,
                "sourceCode": c_code,
                "llmBackend": self.config.llm_backend,
                "action": "submit",
            }
            resp = await client.post(
                "/api/submissions",
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30.0,
            )
            t_after_post = time.perf_counter()
            http_accepted_ms = (t_after_post - t0) * 1000.0

            if resp.status_code == 401:
                # Token expired during run - auto-refresh and retry once
                try:
                    parts = token.split(".")
                    padded = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
                    jwt_data = json.loads(base64.urlsafe_b64decode(padded.encode()))
                    uname = jwt_data.get("preferred_username") or jwt_data.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name")
                    if uname:
                        login_res = await client.post("/api/auth/login", json={"username": uname, "password": self.config.password})
                        if login_res.status_code == 200:
                            token = str(login_res.json().get("accessToken") or login_res.json().get("token"))
                            t_retry = time.perf_counter()
                            resp = await client.post(
                                "/api/submissions",
                                json=payload,
                                headers={"Authorization": f"Bearer {token}"},
                                timeout=30.0,
                            )
                            http_accepted_ms = (time.perf_counter() - t_retry) * 1000.0
                except Exception as refresh_err:
                    logger.warning("Token auto-refresh failed: %s", refresh_err)

            if resp.status_code != 202:
                resp.raise_for_status()

            body = resp.json()
            submission_id = str(body.get("submissionId") or body.get("id"))

            # 2. Concurrently wait via WebSocket and fallback Polling
            ws_url = self._resolve_ws_url(submission_id)
            final_status, terminal_via, events_cnt, err = await self._wait_for_completion(
                submission_id, ws_url, token, client, timeout_s=timeout_s
            )

            client_e2e_ms = (time.perf_counter() - t0) * 1000.0
            completed_epoch = time.time()

            return ClientSubmissionResult(
                submission_id=submission_id,
                token=token,
                step_label=step_label,
                fixture_type=archetype,
                client_e2e_ms=client_e2e_ms,
                http_accepted_ms=http_accepted_ms,
                final_status=final_status,
                error_message=err,
                terminal_via=terminal_via,
                ws_events_count=events_cnt,
                dispatched_epoch=t0_epoch,
                completed_epoch=completed_epoch,
            )
        except Exception as exc:
            client_e2e_ms = (time.perf_counter() - t0) * 1000.0
            completed_epoch = time.time()
            logger.error("Submission failed or errored: %s", exc)
            return ClientSubmissionResult(
                submission_id=submission_id or "unassigned",
                token=token,
                step_label=step_label,
                fixture_type=archetype,
                client_e2e_ms=client_e2e_ms,
                http_accepted_ms=http_accepted_ms,
                final_status="FAILED",
                error_message=str(exc),
                terminal_via="exception",
                dispatched_epoch=t0_epoch,
                completed_epoch=completed_epoch,
            )
        finally:
            # Always return the token back to user pool so user can be reused!
            await self.user_queue.put(token)

    async def _wait_for_completion(
        self,
        submission_id: str,
        ws_url: str,
        token: str,
        client: httpx.AsyncClient,
        timeout_s: float,
    ) -> tuple[str, str, int, str | None]:
        """Dual-wait: WebSocket stream + periodic HTTP polling fallback."""
        event_count = 0
        terminal_status: str | None = None
        terminal_via = "timeout"
        error_msg: str | None = None

        done_event = asyncio.Event()

        async def ws_listener() -> None:
            nonlocal event_count, terminal_status, terminal_via, error_msg
            subprotocols = ["swcp", f"bearer.{token}"]
            headers = {"Authorization": f"Bearer {token}"}

            while not done_event.is_set():
                try:
                    # websockets >= 14 uses additional_headers; < 14 uses extra_headers
                    try:
                        ws_conn = websockets.connect(
                            ws_url,
                            subprotocols=subprotocols,
                            additional_headers=headers,
                            open_timeout=10.0,
                            close_timeout=5.0,
                        )
                    except TypeError:
                        ws_conn = websockets.connect(
                            ws_url,
                            subprotocols=subprotocols,
                            extra_headers=headers,
                            open_timeout=10.0,
                            close_timeout=5.0,
                        )

                    async with ws_conn as ws:
                        async for message in ws:
                            event_count += 1
                            try:
                                data = json.loads(message)
                                evt_type = str(data.get("eventType") or data.get("stage") or "").upper()
                                if evt_type in ("COMPLETED", "FAILED"):
                                    terminal_status = evt_type
                                    terminal_via = "websocket"
                                    error_msg = data.get("errorMessage")
                                    done_event.set()
                                    return
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug("WS connection ended for %s: %s", submission_id, e)

                if done_event.is_set():
                    return
                # Brief pause before retrying WS connection if still awaiting completion
                await asyncio.sleep(2.0)

        async def poll_fallback() -> None:
            nonlocal terminal_status, terminal_via, error_msg
            # Short initial grace window so WebSocket can attach for fast jobs,
            # then 400ms HTTP poll cadence to avoid multi-second quantization steps.
            await asyncio.sleep(0.3)
            while not done_event.is_set():
                try:
                    r = await client.get(
                        f"/api/submissions/{submission_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=10.0,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        st = str(data.get("status") or data.get("Status") or "").upper()
                        if st in ("COMPLETED", "FAILED"):
                            if not done_event.is_set():
                                terminal_status = st
                                terminal_via = "polling"
                                error_msg = data.get("errorMessage") or data.get("ErrorMessage")
                                done_event.set()
                                return
                    elif r.status_code in (401, 403):
                        logger.warning("Auth error polling submission %s: HTTP %d", submission_id, r.status_code)
                except httpx.RequestError as req_err:
                    logger.debug("Transient request error polling %s: %s", submission_id, req_err)
                except Exception as poll_err:
                    logger.debug("Unexpected polling error for %s: %s", submission_id, poll_err)

                await asyncio.sleep(0.4)

        ws_task = asyncio.create_task(ws_listener())
        poll_task = asyncio.create_task(poll_fallback())

        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            terminal_status = "TIMEOUT"
            terminal_via = "timeout"
            error_msg = f"Timed out waiting for completion after {timeout_s}s"
        finally:
            ws_task.cancel()
            poll_task.cancel()
            await asyncio.gather(ws_task, poll_task, return_exceptions=True)

        return terminal_status or "FAILED", terminal_via, event_count, error_msg

    # =========================================================================
    # Arrival Pattern 1: Stepped Poisson
    # =========================================================================
    async def run_stepped_poisson(
        self,
        rates_per_minute: list[float],
        step_duration_s: float = 120.0,
        min_completions: int = 10,
        on_result: Callable[[ClientSubmissionResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[ClientSubmissionResult]:
        """Run stepped Poisson arrival rate plateaus."""
        all_results: list[ClientSubmissionResult] = []
        client_limits = httpx.Limits(max_connections=300, max_keepalive_connections=100, keepalive_expiry=30.0)

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0, limits=client_limits) as client:
            for rate in rates_per_minute:
                step_label = f"poisson_{rate:.1f}rpm"
                logger.info("Starting plateau: %s (target rate: %.1f subs/min, duration: %.0fs)...",
                            step_label, rate, step_duration_s)

                lam = rate / 60.0  # requests per second
                step_start_perf = time.perf_counter()
                step_start_epoch = time.time()
                in_flight: set[asyncio.Task] = set()
                step_results: list[ClientSubmissionResult] = []
                dispatched_count = 0

                while (time.perf_counter() - step_start_perf < step_duration_s) or (dispatched_count < min_completions):
                    # Sample inter-arrival time: delta_t ~ Exp(lambda)
                    inter_arrival_s = -math.log(random.random()) / lam if lam > 0 else 1.0

                    task = asyncio.create_task(self.execute_single_submission(step_label, client))
                    dispatched_count += 1
                    in_flight.add(task)

                    def make_done_callback(t: asyncio.Task) -> Callable[[asyncio.Task], None]:
                        def done_cb(res_task: asyncio.Task) -> None:
                            in_flight.discard(res_task)
                            if not res_task.cancelled() and res_task.exception() is None:
                                res = res_task.result()
                                step_results.append(res)
                                all_results.append(res)
                                if on_result:
                                    asyncio.create_task(on_result(res))
                        return done_cb

                    task.add_done_callback(make_done_callback(task))

                    await asyncio.sleep(inter_arrival_s)

                # Capture active-window queue backlog before draining in-flight tasks
                dispatch_end_epoch = time.time()
                active_duration_s = max(0.1, dispatch_end_epoch - step_start_epoch)
                in_flight_end = len(in_flight)

                # Drain in-flight submissions for this step before advancing
                if in_flight:
                    logger.info("Draining %d in-flight submissions for step %s...", len(in_flight), step_label)
                    await asyncio.gather(*in_flight, return_exceptions=True)

                step_end_epoch = time.time()
                self.step_windows[step_label] = (step_start_epoch, step_end_epoch)
                completed_in_active = sum(
                    1 for r in step_results
                    if r.final_status == "COMPLETED" and r.completed_epoch <= dispatch_end_epoch
                )
                achieved_rpm = completed_in_active / (active_duration_s / 60.0)
                backlog_growth_rpm = in_flight_end / (active_duration_s / 60.0)
                is_sustained = 1 if (
                    achieved_rpm >= 0.85 * rate and backlog_growth_rpm <= max(4.0, 0.15 * rate)
                ) else 0
                self.step_meta[step_label] = {
                    "target_load": float(rate),
                    "achieved_throughput_per_min": achieved_rpm,
                    "in_flight_end": in_flight_end,
                    "backlog_growth_per_min": backlog_growth_rpm,
                    "step_duration_actual_s": active_duration_s,
                    "max_e2e_ms": max((r.client_e2e_ms for r in step_results), default=0.0),
                    "is_sustained": is_sustained,
                }
                duration_actual = step_end_epoch - step_start_epoch
                logger.info(
                    "Completed plateau %s: %d subs (active completed=%d, in_flight_end=%d, achieved=%.1f/min, sustained=%s) in %.1fs.",
                    step_label, len(step_results), completed_in_active, in_flight_end, achieved_rpm, bool(is_sustained), duration_actual,
                )
                await asyncio.sleep(2.0)

        return all_results

    # =========================================================================
    # Arrival Pattern 1C: Continuous Non-Homogeneous Ramp (Thinning)
    # =========================================================================
    async def run_continuous_ramp(
        self,
        start_rate: float = 30.0,
        end_rate: float = 260.0,
        ramp_duration_s: float = 480.0,
        bucket_duration_s: float = 30.0,
        stop_on_saturation: bool = False,
        saturation_confirm_buckets: int = 3,
        saturation_deficit_ratio: float = 0.12,
        min_saturation_buckets: int = 4,
        on_result: Callable[[ClientSubmissionResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[ClientSubmissionResult]:
        """Run a continuous linear-rate Poisson ramp using exact Lewis-Shedler thinning."""
        all_results: list[ClientSubmissionResult] = []
        client_limits = httpx.Limits(max_connections=400, max_keepalive_connections=150, keepalive_expiry=30.0)

        ramp_duration_s = max(10.0, float(ramp_duration_s))
        bucket_duration_s = max(5.0, float(bucket_duration_s))
        num_buckets = max(1, math.ceil(ramp_duration_s / bucket_duration_s))

        buckets: list[dict[str, Any]] = []
        for idx in range(num_buckets):
            rel_start = idx * bucket_duration_s
            rel_end = min(ramp_duration_s, (idx + 1) * bucket_duration_s)
            mid_frac = (0.5 * (rel_start + rel_end)) / ramp_duration_s
            mid_rate = start_rate + (end_rate - start_rate) * mid_frac
            buckets.append({
                "label": f"ramp_{idx + 1:02d}_{mid_rate:.0f}rpm",
                "rel_start": rel_start,
                "rel_end": rel_end,
                "mid_rate": mid_rate,
                "in_flight_end": 0,
            })

        lam_max = max(start_rate, end_rate, 0.1) / 60.0
        in_flight: set[asyncio.Task] = set()
        executed_buckets: list[dict[str, Any]] = []
        stop_event = asyncio.Event()
        ramp_start_perf = time.perf_counter()
        ramp_start_epoch = time.time()

        logger.info(
            "Starting continuous ramp: %.1f -> %.1f subs/min over %.0fs (%d buckets of %.0fs, auto_stop=%s)...",
            start_rate, end_rate, ramp_duration_s, num_buckets, bucket_duration_s, stop_on_saturation,
        )

        async def _bucket_backlog_sampler() -> None:
            saturation_streak = 0
            prev_backlog = 0
            for b in buckets:
                wait_s = (ramp_start_perf + b["rel_end"]) - time.perf_counter()
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                b_start_epoch = ramp_start_epoch + b["rel_start"]
                b_end_epoch = ramp_start_epoch + b["rel_end"]
                b_dur_s = max(0.1, b_end_epoch - b_start_epoch)
                in_flight_end = len(in_flight)
                b["in_flight_end"] = in_flight_end
                executed_buckets.append(b)

                completed_in_window = sum(
                    1 for r in all_results
                    if r.final_status == "COMPLETED" and b_start_epoch <= r.completed_epoch < b_end_epoch
                )
                achieved_rpm = completed_in_window / (b_dur_s / 60.0)
                min_queue_floor = max(8, int(round(0.06 * b["mid_rate"])))
                is_growing = (in_flight_end > prev_backlog) and (in_flight_end >= min_queue_floor)
                is_behind = achieved_rpm < (1.0 - saturation_deficit_ratio) * b["mid_rate"]

                if stop_on_saturation and len(executed_buckets) >= min_saturation_buckets:
                    saturation_streak = (saturation_streak + 1) if (is_growing and is_behind) else 0
                else:
                    saturation_streak = 0

                sat_msg = (
                    f", sat_streak={saturation_streak}/{saturation_confirm_buckets}"
                    if stop_on_saturation else ""
                )
                logger.info(
                    "Ramp bucket %s finished (rate=%.1f/min, completed=%.1f/min, active backlog=%d%s)",
                    b["label"], b["mid_rate"], achieved_rpm, in_flight_end, sat_msg,
                )

                prev_backlog = in_flight_end
                if stop_on_saturation and saturation_streak >= max(1, saturation_confirm_buckets):
                    logger.info(
                        "Sustained saturation confirmed for %d consecutive buckets at %.1f subs/min — stopping continuous ramp early.",
                        saturation_streak, b["mid_rate"],
                    )
                    stop_event.set()
                    break

        sampler_task = asyncio.create_task(_bucket_backlog_sampler())

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0, limits=client_limits) as client:
            while not stop_event.is_set():
                elapsed = time.perf_counter() - ramp_start_perf
                if elapsed >= ramp_duration_s:
                    break

                dt = -math.log(max(1e-12, random.random())) / lam_max
                if elapsed + dt >= ramp_duration_s:
                    rem = ramp_duration_s - elapsed
                    if rem > 0:
                        try:
                            await asyncio.wait_for(stop_event.wait(), timeout=rem)
                        except asyncio.TimeoutError:
                            pass
                    break

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=dt)
                    break
                except asyncio.TimeoutError:
                    pass

                if stop_event.is_set():
                    break

                elapsed_now = time.perf_counter() - ramp_start_perf
                if elapsed_now >= ramp_duration_s:
                    break

                inst_rate = start_rate + (end_rate - start_rate) * (elapsed_now / ramp_duration_s)
                accept_prob = inst_rate / max(start_rate, end_rate, 0.1)
                if random.random() > accept_prob:
                    continue

                b_idx = min(num_buckets - 1, int(elapsed_now // bucket_duration_s))
                step_label = buckets[b_idx]["label"]
                task = asyncio.create_task(self.execute_single_submission(step_label, client, timeout_s=360.0))
                in_flight.add(task)

                def _done_cb(res_task: asyncio.Task) -> None:
                    in_flight.discard(res_task)
                    if not res_task.cancelled() and res_task.exception() is None:
                        res = res_task.result()
                        all_results.append(res)
                        if on_result:
                            asyncio.create_task(on_result(res))

                task.add_done_callback(_done_cb)

            await sampler_task
            if in_flight:
                logger.info("Draining %d remaining submissions after continuous ramp...", len(in_flight))
                await asyncio.gather(*in_flight, return_exceptions=True)

        buckets_to_summarize = executed_buckets if executed_buckets else buckets
        executed_labels = {b["label"] for b in buckets_to_summarize}
        fallback_label = buckets_to_summarize[-1]["label"]
        for r in all_results:
            if r.step_label not in executed_labels:
                r.step_label = fallback_label

        prev_backlog = 0
        for b in buckets_to_summarize:
            b_start_epoch = ramp_start_epoch + b["rel_start"]
            b_end_epoch = ramp_start_epoch + b["rel_end"]
            b_dur_s = max(0.1, b_end_epoch - b_start_epoch)
            label = b["label"]
            mid_rate = b["mid_rate"]
            in_flight_end = int(b["in_flight_end"])

            completed_in_window = sum(
                1 for r in all_results
                if r.final_status == "COMPLETED" and b_start_epoch <= r.completed_epoch < b_end_epoch
            )
            bucket_dispatched = [r for r in all_results if r.step_label == label]
            achieved_rpm = completed_in_window / (b_dur_s / 60.0)
            backlog_growth_rpm = (in_flight_end - prev_backlog) / (b_dur_s / 60.0)
            is_sustained = 1 if (
                achieved_rpm >= 0.85 * mid_rate and backlog_growth_rpm <= max(4.0, 0.15 * mid_rate)
            ) else 0

            self.step_windows[label] = (b_start_epoch, b_end_epoch)
            self.step_meta[label] = {
                "target_load": float(mid_rate),
                "achieved_throughput_per_min": achieved_rpm,
                "in_flight_end": in_flight_end,
                "backlog_growth_per_min": backlog_growth_rpm,
                "step_duration_actual_s": b_dur_s,
                "max_e2e_ms": max((r.client_e2e_ms for r in bucket_dispatched), default=0.0),
                "is_sustained": is_sustained,
            }
            prev_backlog = in_flight_end

        return all_results

    def _finalize_poisson_step_meta(
        self,
        step_label: str,
        target_rate: float,
        step_start_epoch: float,
        dispatch_end_epoch: float,
        step_results: list[ClientSubmissionResult],
        in_flight_end: int,
    ) -> None:
        active_duration_s = max(0.1, dispatch_end_epoch - step_start_epoch)
        completed_in_active = sum(
            1 for r in step_results
            if r.final_status == "COMPLETED" and r.completed_epoch <= dispatch_end_epoch
        )
        achieved_rpm = completed_in_active / (active_duration_s / 60.0)
        backlog_growth_rpm = in_flight_end / (active_duration_s / 60.0)
        is_sustained = 1 if (
            achieved_rpm >= 0.85 * target_rate and backlog_growth_rpm <= max(4.0, 0.15 * target_rate)
        ) else 0
        self.step_meta[step_label] = {
            "target_load": float(target_rate),
            "achieved_throughput_per_min": achieved_rpm,
            "in_flight_end": in_flight_end,
            "backlog_growth_per_min": backlog_growth_rpm,
            "step_duration_actual_s": active_duration_s,
            "max_e2e_ms": max((r.client_e2e_ms for r in step_results), default=0.0),
            "is_sustained": is_sustained,
        }

    # =========================================================================
    # Arrival Pattern 1B: Adaptive Stepped Poisson (Knee-Finding)
    # =========================================================================
    async def run_adaptive_poisson(
        self,
        start_rate: float = 10.0,
        rate_delta: float = 5.0,
        max_rate: float = 0.0,
        target_p95_s: float = 30.0,
        step_duration_s: float = 90.0,
        min_completions: int = 8,
        confirmation_steps: int = 1,
        on_result: Callable[[ClientSubmissionResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[ClientSubmissionResult]:
        """Dynamically ramp Poisson arrival rate until p95 exceeds target_p95_s, plus confirmation."""
        all_results: list[ClientSubmissionResult] = []
        client_limits = httpx.Limits(max_connections=300, max_keepalive_connections=100, keepalive_expiry=30.0)

        current_rate = start_rate
        confirmation_done = 0

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0, limits=client_limits) as client:
            while max_rate <= 0 or current_rate <= max_rate:
                step_label = f"poisson_{current_rate:.1f}rpm"
                logger.info(
                    "Starting adaptive plateau: %s (duration: %.0fs, target p95: %.1fs)...",
                    step_label, step_duration_s, target_p95_s
                )

                lam = current_rate / 60.0
                step_start_perf = time.perf_counter()
                step_start_epoch = time.time()
                in_flight: set[asyncio.Task] = set()
                step_results: list[ClientSubmissionResult] = []
                dispatched_count = 0

                while (time.perf_counter() - step_start_perf < step_duration_s) or (dispatched_count < min_completions):
                    inter_arrival_s = -math.log(random.random()) / lam if lam > 0 else 1.0

                    task = asyncio.create_task(self.execute_single_submission(step_label, client))
                    dispatched_count += 1
                    in_flight.add(task)

                    def make_done_callback(t: asyncio.Task) -> Callable[[asyncio.Task], None]:
                        def done_cb(res_task: asyncio.Task) -> None:
                            in_flight.discard(res_task)
                            if not res_task.cancelled() and res_task.exception() is None:
                                res = res_task.result()
                                step_results.append(res)
                                all_results.append(res)
                                if on_result:
                                    asyncio.create_task(on_result(res))
                        return done_cb

                    task.add_done_callback(make_done_callback(task))
                    await asyncio.sleep(inter_arrival_s)

                dispatch_end_epoch = time.time()
                in_flight_end = len(in_flight)

                if in_flight:
                    logger.info("Draining %d in-flight submissions for step %s...", len(in_flight), step_label)
                    await asyncio.gather(*in_flight, return_exceptions=True)

                step_end_epoch = time.time()
                self.step_windows[step_label] = (step_start_epoch, step_end_epoch)
                self._finalize_poisson_step_meta(
                    step_label, current_rate, step_start_epoch, dispatch_end_epoch, step_results, in_flight_end
                )
                duration_actual = step_end_epoch - step_start_epoch

                # Calculate step p95 latency
                e2e_vals = [r.client_e2e_ms for r in step_results]
                step_p95_s = (float(np.percentile(e2e_vals, 95)) / 1000.0) if e2e_vals else 0.0
                step_p50_s = (float(np.percentile(e2e_vals, 50)) / 1000.0) if e2e_vals else 0.0

                logger.info(
                    "Completed plateau %s: %d subs in %.1fs | p50: %.2fs, p95: %.2fs (SLA Target: %.1fs)",
                    step_label, len(step_results), duration_actual, step_p50_s, step_p95_s, target_p95_s
                )

                if step_p95_s >= target_p95_s:
                    if confirmation_done < confirmation_steps:
                        confirmation_done += 1
                        logger.info(
                            "Knee reached (p95=%.2fs >= %.1fs). Running %d confirmation step(s) at higher load...",
                            step_p95_s, target_p95_s, confirmation_steps - confirmation_done + 1
                        )
                        current_rate += rate_delta
                        await asyncio.sleep(2.0)
                        continue
                    else:
                        logger.info(
                            "Adaptive saturation limit confirmed (p95=%.2fs >= %.1fs). Halting ramp.",
                            step_p95_s, target_p95_s
                        )
                        break

                current_rate += rate_delta
                await asyncio.sleep(2.0)

        return all_results

    # =========================================================================
    # Arrival Pattern 2: Closed-Loop (Virtual Concurrent Students)
    # =========================================================================
    async def run_closed_loop(
        self,
        concurrency_levels: list[int],
        step_duration_s: float = 120.0,
        think_time_min_s: float = 10.0,
        think_time_max_s: float = 20.0,
        on_result: Callable[[ClientSubmissionResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[ClientSubmissionResult]:
        """Run closed-loop virtual students who submit, wait, think, and repeat."""
        all_results: list[ClientSubmissionResult] = []
        client_limits = httpx.Limits(max_connections=300, max_keepalive_connections=100, keepalive_expiry=30.0)

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0, limits=client_limits) as client:
            for users_count in concurrency_levels:
                step_label = f"closed_loop_{users_count}users"
                logger.info("Starting closed-loop plateau: %s (duration: %.0fs)...", step_label, step_duration_s)

                stop_event = asyncio.Event()
                step_start_epoch = time.time()

                async def virtual_student() -> None:
                    while not stop_event.is_set():
                        res = await self.execute_single_submission(step_label, client)
                        all_results.append(res)
                        if on_result:
                            await on_result(res)
                        if stop_event.is_set():
                            break
                        if think_time_max_s > 0.0 or think_time_min_s > 0.0:
                            think_s = random.uniform(max(0.0, think_time_min_s), max(think_time_min_s, think_time_max_s))
                            if think_s > 0.0:
                                await asyncio.sleep(think_s)

                workers = [asyncio.create_task(virtual_student()) for _ in range(users_count)]
                await asyncio.sleep(step_duration_s)
                stop_event.set()
                await asyncio.gather(*workers, return_exceptions=True)
                step_end_epoch = time.time()
                self.step_windows[step_label] = (step_start_epoch, step_end_epoch)
                logger.info("Completed closed-loop step %s in %.1fs.", step_label, step_end_epoch - step_start_epoch)
                await asyncio.sleep(2.0)

        return all_results

    # =========================================================================
    # Arrival Pattern 3: Synchronized Burst ("Thundering Herd")
    # =========================================================================
    async def run_burst(
        self,
        burst_sizes: list[int],
        burst_window_s: float = 5.0,
        drain_timeout_s: float = 240.0,
        on_result: Callable[[ClientSubmissionResult], Coroutine[Any, Any, None]] | None = None,
    ) -> list[ClientSubmissionResult]:
        """Fire B simultaneous submissions within a short burst window."""
        all_results: list[ClientSubmissionResult] = []
        client_limits = httpx.Limits(max_connections=300, max_keepalive_connections=100, keepalive_expiry=30.0)

        async with httpx.AsyncClient(base_url=self.config.target_url, timeout=30.0, limits=client_limits) as client:
            for b_size in burst_sizes:
                step_label = f"burst_{b_size}subs"
                logger.info("Firing synchronized burst of %d submissions over %.1fs window...", b_size, burst_window_s)

                step_start_epoch = time.time()
                interval = burst_window_s / max(1, b_size)
                tasks: list[asyncio.Task] = []

                for _ in range(b_size):
                    t = asyncio.create_task(self.execute_single_submission(step_label, client, timeout_s=drain_timeout_s))
                    tasks.append(t)
                    if interval > 0.01:
                        await asyncio.sleep(interval)

                burst_results = await asyncio.gather(*tasks, return_exceptions=False)
                for res in burst_results:
                    all_results.append(res)
                    if on_result:
                        await on_result(res)

                step_end_epoch = time.time()
                drain_wall_ms = (step_end_epoch - step_start_epoch) * 1000.0
                self.step_windows[step_label] = (step_start_epoch, step_end_epoch)
                self.step_meta[step_label] = {
                    "target_load": float(b_size),
                    "achieved_throughput_per_min": len(burst_results) / max(0.01, (step_end_epoch - step_start_epoch) / 60.0),
                    "in_flight_end": 0,
                    "backlog_growth_per_min": 0.0,
                    "step_duration_actual_s": step_end_epoch - step_start_epoch,
                    "max_e2e_ms": drain_wall_ms,
                    "is_sustained": 0,
                }
                logger.info("Burst step %s finished: %d submissions drained in %.1fs.", step_label, len(burst_results), step_end_epoch - step_start_epoch)
                await asyncio.sleep(2.0)

        return all_results

