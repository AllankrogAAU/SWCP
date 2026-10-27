"""Telemetry scraper for cluster Loki (stage logs) and Prometheus (GPU/cluster metrics)."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from benchmarks.config import BenchmarkConfig

logger = logging.getLogger("benchmarks.telemetry")


@dataclass
class StageTimings:
    submission_id: str
    http_enqueue_ms: float = 0.0
    sandbox_queue_wait_ms: float = 0.0
    sandbox_exec_ms: float = 0.0
    llm_queue_wait_ms: float = 0.0
    llm_exec_ms: float = 0.0
    pipeline_total_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_sec: float = 0.0
    final_status: str = "COMPLETED"
    outcome: str = ""


@dataclass
class PrometheusSnapshot:
    peak_kv_cache_pct: float = 0.0
    mean_tok_per_s: float = 0.0
    max_replicas_sandbox: int = 3
    max_replicas_llm_worker: int = 2
    max_replicas_core_api: int = 2
    timeseries: list[dict[str, Any]] = field(default_factory=list)


def resolve_prometheus_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env_url = os.getenv("PROMETHEUS_URL")
    if env_url:
        return env_url.rstrip("/")
    for candidate in (
        "http://10.43.80.66:9090",
        "http://localhost:9090",
        "http://prometheus.monitoring.svc.cluster.local:9090",
    ):
        try:
            req = urllib.request.Request(f"{candidate}/-/healthy")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return candidate
        except Exception:
            pass
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "svc", "prometheus", "-n", "monitoring", "-o", "jsonpath={.spec.clusterIP}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return f"http://{out}:9090"
    except Exception:
        pass
    return "http://localhost:9090"


def resolve_loki_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    env_url = os.getenv("LOKI_URL")
    if env_url:
        return env_url.rstrip("/")
    for candidate in (
        "http://10.43.114.122:3100",
        "http://localhost:3100",
        "http://loki.monitoring.svc.cluster.local:3100",
    ):
        try:
            req = urllib.request.Request(f"{candidate}/ready")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return candidate
        except Exception:
            pass
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "svc", "loki", "-n", "monitoring", "-o", "jsonpath={.spec.clusterIP}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return f"http://{out}:3100"
    except Exception:
        pass
    return "http://localhost:3100"


class TelemetryCollector:
    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.prom_url = resolve_prometheus_url(config.prometheus_url)
        self.loki_url = resolve_loki_url(config.loki_url)
        logger.info("Telemetry initialized: Prometheus=%s, Loki=%s", self.prom_url, self.loki_url)

    def _http_get_json(self, url: str, timeout: float = 10.0) -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug("HTTP GET %s failed: %s", url, e)
            return None

    def collect_submission_stages(
        self,
        submission_id: str,
        start_epoch: float,
        end_epoch: float,
    ) -> StageTimings:
        """Query Loki for structured stage logs for a single submission."""
        timings = StageTimings(submission_id=submission_id)

        # Buffer timestamps slightly to ensure no window boundary edge loss
        start_ns = int((start_epoch - 10.0) * 1_000_000_000)
        end_ns = int((end_epoch + 15.0) * 1_000_000_000)

        query = f'{{namespace="swcp"}} |= "{submission_id}"'
        params = urllib.parse.urlencode({
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": "200",
            "direction": "FORWARD",
        })
        url = f"{self.loki_url}/loki/api/v1/query_range?{params}"
        data = self._http_get_json(url)

        if not data or not data.get("data", {}).get("result"):
            return timings

        for stream in data["data"]["result"]:
            for entry in stream.get("values", []):
                line = entry[1]
                idx = line.find("{")
                if idx == -1:
                    continue
                try:
                    payload = json.loads(line[idx:])
                    state = payload.get("State") or payload
                    evt = str(state.get("event_name", ""))
                    stage = str(state.get("stage", ""))

                    if evt == "pipeline.stage.completed" and stage == "submission_enqueue":
                        val = state.get("duration_ms")
                        if val is not None:
                            timings.http_enqueue_ms = float(val)

                    elif evt == "pipeline.stage.started" and stage == "sandbox":
                        val = state.get("queue_wait_ms")
                        if val is not None:
                            timings.sandbox_queue_wait_ms = float(val)

                    elif evt == "pipeline.stage.completed" and stage == "sandbox":
                        val = state.get("duration_ms")
                        if val is not None:
                            timings.sandbox_exec_ms = float(val)

                    elif evt == "pipeline.stage.started" and stage == "llm":
                        val = state.get("queue_wait_ms")
                        if val is not None:
                            timings.llm_queue_wait_ms = float(val)

                    elif evt == "pipeline.stage.completed" and stage == "llm":
                        val = state.get("duration_ms")
                        if val is not None:
                            timings.llm_exec_ms = float(val)
                        if state.get("prompt_tokens") is not None:
                            timings.prompt_tokens = int(state["prompt_tokens"])
                        if state.get("completion_tokens") is not None:
                            timings.completion_tokens = int(state["completion_tokens"])
                        if state.get("tokens_per_second") is not None:
                            timings.tokens_per_sec = float(state["tokens_per_second"])

                    elif evt == "pipeline.completed":
                        val = state.get("duration_ms")
                        if val is not None:
                            timings.pipeline_total_ms = float(val)
                        timings.final_status = str(state.get("status", "completed")).upper()
                        timings.outcome = str(state.get("outcome", ""))
                except Exception:
                    pass

        return timings

    def collect_prometheus_snapshot(
        self,
        start_epoch: float,
        end_epoch: float,
        step_s: int = 5,
    ) -> PrometheusSnapshot:
        """Query Prometheus for GPU and pod scaling metrics over run window."""
        snapshot = PrometheusSnapshot()

        # 1. Query peak KV cache %
        kv_data = self._query_prom_range(
            'vllm:kv_cache_usage_perc{namespace="swcp"}',
            start_epoch, end_epoch, step_s
        )
        if kv_data:
            vals = [float(v[1]) for series in kv_data for v in series.get("values", [])]
            if vals:
                snapshot.peak_kv_cache_pct = max(vals) * (100.0 if max(vals) <= 1.0 else 1.0)

        # 2. Query mean token generation rate
        tok_data = self._query_prom_range(
            'rate(vllm:num_total_tokens_total{namespace="swcp"}[30s])',
            start_epoch, end_epoch, step_s
        )
        if tok_data:
            vals = [float(v[1]) for series in tok_data for v in series.get("values", [])]
            if vals:
                snapshot.mean_tok_per_s = sum(vals) / len(vals)

        # 3. Query replica counts per service
        rep_data = self._query_prom_range(
            'count by (app_kubernetes_io_name) (kube_pod_status_phase{namespace="swcp",phase="Running"})',
            start_epoch, end_epoch, step_s
        )
        if rep_data:
            for series in rep_data:
                name = series.get("metric", {}).get("app_kubernetes_io_name", "")
                vals = [int(float(v[1])) for v in series.get("values", [])]
                if not vals:
                    continue
                max_val = max(vals)
                if "sandbox" in name:
                    snapshot.max_replicas_sandbox = max(snapshot.max_replicas_sandbox, max_val)
                elif "llm-worker" in name:
                    snapshot.max_replicas_llm_worker = max(snapshot.max_replicas_llm_worker, max_val)
                elif "core-api" in name:
                    snapshot.max_replicas_core_api = max(snapshot.max_replicas_core_api, max_val)

        return snapshot

    def _query_prom_range(
        self,
        query: str,
        start_epoch: float,
        end_epoch: float,
        step_s: int = 5,
    ) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({
            "query": query,
            "start": str(start_epoch),
            "end": str(end_epoch),
            "step": f"{step_s}s",
        })
        url = f"{self.prom_url}/api/v1/query_range?{params}"
        res = self._http_get_json(url)
        if res and res.get("status") == "success":
            return res.get("data", {}).get("result", [])
        return []
