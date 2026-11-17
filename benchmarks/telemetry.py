"""Telemetry scraper for cluster Loki (stage logs) and Prometheus (GPU/cluster metrics)."""

from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import subprocess
import time
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
    llm_target: str = ""
    sandbox_node: str = ""
    llm_worker_node: str = ""


@dataclass
class PrometheusSnapshot:
    peak_kv_cache_pct: float = 0.0
    primary_kv_cache_pct: float = 0.0
    overflow_kv_cache_pct: float = 0.0
    mean_tok_per_s: float = 0.0
    primary_mean_tok_per_s: float = 0.0
    overflow_mean_tok_per_s: float = 0.0
    primary_requests: int = 0
    overflow_requests: int = 0
    max_replicas_sandbox: int = 3
    max_replicas_llm_worker: int = 2
    max_replicas_core_api: int = 2
    max_replicas_sandbox_worker: int = 0
    max_replicas_llm_worker_worker: int = 0
    max_replicas_core_api_worker: int = 0
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
        self.clock_offset_s = 0.0
        self.target_http_rtt_ms = 0.0
        logger.info("Telemetry initialized: Prometheus=%s, Loki=%s", self.prom_url, self.loki_url)

    def measure_target_baseline(self) -> tuple[float, float]:
        """Measure client->target HTTP RTT (ms) and server-minus-client clock offset (s) via HTTP Date header."""
        target = self.config.target_url.rstrip("/")
        samples_rtt: list[float] = []
        offsets: list[float] = []
        for _ in range(3):
            try:
                t0 = time.perf_counter()
                epoch_before = time.time()
                req = urllib.request.Request(f"{target}/", method="GET")
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    resp.read(256)
                    rtt_ms = (time.perf_counter() - t0) * 1000.0
                    epoch_mid = 0.5 * (epoch_before + time.time())
                    date_hdr = resp.headers.get("Date")
                    samples_rtt.append(rtt_ms)
                    if date_hdr:
                        server_dt = email.utils.parsedate_to_datetime(date_hdr)
                        offsets.append(server_dt.timestamp() - epoch_mid)
            except Exception as e:
                logger.debug("Baseline probe to %s failed: %s", target, e)

        if samples_rtt:
            self.target_http_rtt_ms = round(sum(samples_rtt) / len(samples_rtt), 2)
        if offsets:
            avg_offset = sum(offsets) / len(offsets)
            # Ignore < 1.5s jitter (HTTP Date only has 1-second resolution)
            self.clock_offset_s = round(avg_offset, 2) if abs(avg_offset) >= 1.5 else 0.0
        if self.target_http_rtt_ms or self.clock_offset_s:
            logger.info(
                "Target baseline (%s): HTTP RTT=%.2f ms, clock_offset=%.2fs",
                target, self.target_http_rtt_ms, self.clock_offset_s,
            )
        return self.target_http_rtt_ms, self.clock_offset_s

    def _http_get_json(self, url: str, timeout: float = 10.0) -> dict[str, Any] | None:
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            logger.debug("HTTP GET %s failed: %s", url, e)
            return None

    @staticmethod
    def _apply_log_entry(timings: StageTimings, state: dict[str, Any], stream_node: str = "") -> None:
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
            if stream_node and not timings.sandbox_node:
                timings.sandbox_node = stream_node

        elif evt == "pipeline.stage.started" and stage == "llm":
            val = state.get("queue_wait_ms")
            if val is not None:
                timings.llm_queue_wait_ms = float(val)

        elif evt == "pipeline.stage.completed" and stage == "llm":
            val = state.get("duration_ms")
            if val is not None:
                timings.llm_exec_ms = float(val)
            if state.get("llm_target") is not None:
                timings.llm_target = str(state["llm_target"])
            if stream_node and not timings.llm_worker_node:
                timings.llm_worker_node = stream_node
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

    def collect_submission_stages(
        self,
        submission_id: str,
        start_epoch: float,
        end_epoch: float,
    ) -> StageTimings:
        """Query Loki for structured stage logs for a single submission."""
        timings = StageTimings(submission_id=submission_id)

        # Buffer timestamps slightly and align to cluster UTC if client clock differs
        start_ns = int((start_epoch + self.clock_offset_s - 10.0) * 1_000_000_000)
        end_ns = int((end_epoch + self.clock_offset_s + 15.0) * 1_000_000_000)

        # Filter to structured pipeline.* stage events so high-frequency HTTP polling
        # logs (api-gateway/core-api) cannot truncate stage timings via Loki's limit.
        query = f'{{namespace="swcp"}} |= "{submission_id}" |= "pipeline."'
        params = urllib.parse.urlencode({
            "query": query,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": "500",
            "direction": "FORWARD",
        })
        url = f"{self.loki_url}/loki/api/v1/query_range?{params}"
        data = self._http_get_json(url)

        if not data or not data.get("data", {}).get("result"):
            return timings

        for stream in data["data"]["result"]:
            stream_labels = stream.get("stream", {})
            stream_node = str(stream_labels.get("node", ""))
            for entry in stream.get("values", []):
                line = entry[1]
                idx = line.find("{")
                if idx == -1:
                    continue
                try:
                    payload = json.loads(line[idx:])
                    state = payload.get("State") or payload.get("Properties") or payload
                    self._apply_log_entry(timings, state, stream_node)
                except Exception:
                    pass

        return timings

    def collect_batch_submission_stages(
        self,
        submission_ids: list[str] | set[str],
        start_epoch: float,
        end_epoch: float,
        chunk_seconds: float = 15.0,
    ) -> dict[str, StageTimings]:
        """Query Loki in chunked time windows for all pipeline stage events,

        and assemble StageTimings for each submission in memory in seconds.
        Chunking ensures Loki's server max_entries_limit (5000) is never exceeded.
        """
        sub_id_set = {str(s).lower() for s in submission_ids}
        timings_by_sub: dict[str, StageTimings] = {
            s: StageTimings(submission_id=s) for s in submission_ids
        }
        if not sub_id_set:
            return timings_by_sub

        uuid_pattern = re.compile(
            r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
            re.IGNORECASE,
        )

        padded_start = start_epoch + self.clock_offset_s - 10.0
        padded_end = end_epoch + self.clock_offset_s + 15.0
        cur_start = padded_start
        while cur_start < padded_end:
            cur_end = min(padded_end, cur_start + chunk_seconds)
            start_ns = int(cur_start * 1_000_000_000)
            end_ns = int(cur_end * 1_000_000_000)

            query = '{namespace="swcp"} |= "pipeline."'
            params = urllib.parse.urlencode({
                "query": query,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": "5000",
                "direction": "FORWARD",
            })
            url = f"{self.loki_url}/loki/api/v1/query_range?{params}"
            data = self._http_get_json(url, timeout=30.0)

            if data and data.get("data", {}).get("result"):
                for stream in data["data"]["result"]:
                    stream_labels = stream.get("stream", {})
                    stream_node = str(stream_labels.get("node", ""))
                    for entry in stream.get("values", []):
                        line = entry[1]
                        idx = line.find("{")
                        if idx == -1:
                            continue
                        try:
                            payload = json.loads(line[idx:])
                            state = payload.get("State") or payload.get("Properties") or payload

                            sub_id = (
                                state.get("submission_id")
                                or state.get("SubmissionId")
                                or payload.get("submission_id")
                                or (payload.get("Properties", {}).get("submission_id") if isinstance(payload.get("Properties"), dict) else None)
                            )
                            if not sub_id:
                                m = uuid_pattern.search(line)
                                if m:
                                    sub_id = m.group(0)

                            if not sub_id:
                                continue

                            sub_id_str = str(sub_id).lower()
                            if sub_id_str not in sub_id_set:
                                continue

                            target_timings = timings_by_sub.get(sub_id) or timings_by_sub.get(sub_id_str)
                            if target_timings:
                                self._apply_log_entry(target_timings, state, stream_node)
                        except Exception:
                            pass

            cur_start = cur_end

        return timings_by_sub

    @staticmethod
    def _vllm_role(metric: dict[str, Any]) -> str:
        role = str(metric.get("swcp_io_vllm_role", "")).lower()
        if role in ("primary", "overflow"):
            return role
        pod = str(metric.get("pod", "")).lower()
        return "overflow" if "overflow" in pod else "primary"

    def _discover_control_plane_nodes(self) -> set[str]:
        url = f"{self.prom_url}/api/v1/query?{urllib.parse.urlencode({'query': 'up{job=\"kubernetes-nodes\",node_role_kubernetes_io_control_plane=\"true\"}'})}"
        res = self._http_get_json(url)
        if res and res.get("status") == "success":
            nodes = {
                str(item.get("metric", {}).get("node", ""))
                for item in res.get("data", {}).get("result", [])
            }
            nodes.discard("")
            if nodes:
                return nodes
        return {"control-node"}

    def collect_prometheus_snapshot(
        self,
        start_epoch: float,
        end_epoch: float,
        step_s: int = 5,
    ) -> PrometheusSnapshot:
        """Query Prometheus for GPU and pod scaling metrics over run window."""
        snapshot = PrometheusSnapshot()
        prom_start = start_epoch + self.clock_offset_s
        prom_end = end_epoch + self.clock_offset_s

        # 1. Query peak KV cache % per vLLM role (primary vs overflow)
        kv_data = self._query_prom_range(
            'vllm:kv_cache_usage_perc{namespace="swcp"}',
            prom_start, prom_end, step_s
        )
        if kv_data:
            primary_vals: list[float] = []
            overflow_vals: list[float] = []
            for series in kv_data:
                role = self._vllm_role(series.get("metric", {}))
                vals = [float(v[1]) * (100.0 if float(v[1]) <= 1.0 else 1.0) for v in series.get("values", [])]
                if role == "overflow":
                    overflow_vals.extend(vals)
                else:
                    primary_vals.extend(vals)
            if primary_vals:
                snapshot.primary_kv_cache_pct = max(primary_vals)
            if overflow_vals:
                snapshot.overflow_kv_cache_pct = max(overflow_vals)
            snapshot.peak_kv_cache_pct = max(snapshot.primary_kv_cache_pct, snapshot.overflow_kv_cache_pct)

        # 2. Query summed token generation rate across active vLLM nodes
        tok_data = self._query_prom_range(
            'rate(vllm:generation_tokens_total{namespace="swcp"}[30s])',
            prom_start, prom_end, step_s
        )
        if tok_data:
            total_by_ts: dict[float, float] = {}
            by_role: dict[str, dict[float, float]] = {"primary": {}, "overflow": {}}
            for series in tok_data:
                role = self._vllm_role(series.get("metric", {}))
                for ts, raw_val in series.get("values", []):
                    t_key = float(ts)
                    val = max(0.0, float(raw_val))
                    total_by_ts[t_key] = total_by_ts.get(t_key, 0.0) + val
                    by_role[role][t_key] = by_role[role].get(t_key, 0.0) + val
            active_total = [v for v in total_by_ts.values() if v > 0.1]
            all_total = list(total_by_ts.values())
            if active_total:
                snapshot.mean_tok_per_s = sum(active_total) / len(active_total)
            elif all_total:
                snapshot.mean_tok_per_s = sum(all_total) / len(all_total)
            prim_active = [v for v in by_role["primary"].values() if v > 0.1]
            ovf_active = [v for v in by_role["overflow"].values() if v > 0.1]
            if prim_active:
                snapshot.primary_mean_tok_per_s = sum(prim_active) / len(prim_active)
            if ovf_active:
                snapshot.overflow_mean_tok_per_s = sum(ovf_active) / len(ovf_active)

        # 3. Query completed request counts per vLLM role over window
        req_data = self._query_prom_range(
            'vllm:e2e_request_latency_seconds_count{namespace="swcp"}',
            prom_start, prom_end, step_s
        )
        if req_data:
            for series in req_data:
                role = self._vllm_role(series.get("metric", {}))
                vals = [float(v[1]) for v in series.get("values", [])]
                if len(vals) >= 2:
                    delta = max(0, int(round(vals[-1] - vals[0])))
                    if role == "overflow":
                        snapshot.overflow_requests += delta
                    else:
                        snapshot.primary_requests += delta

        # 4. Query simultaneous replica counts per service and node
        control_nodes = self._discover_control_plane_nodes()
        rep_data = self._query_prom_range(
            'count by (pod, node) (container_cpu_usage_seconds_total{namespace="swcp",container!="",container!="POD"})',
            prom_start, prom_end, step_s
        )
        if rep_data:
            # Map timestamp -> (service, is_worker_node) -> set of pod names
            active_at_ts: dict[float, dict[tuple[str, bool], set[str]]] = {}
            for series in rep_data:
                metric = series.get("metric", {})
                pod_name = str(metric.get("pod", ""))
                node_name = str(metric.get("node", ""))
                if "c-sandbox" in pod_name:
                    service = "c-sandbox"
                elif "llm-worker" in pod_name:
                    service = "llm-worker"
                elif "core-api" in pod_name:
                    service = "core-api"
                else:
                    continue
                is_worker = bool(node_name) and node_name not in control_nodes
                for ts, _ in series.get("values", []):
                    t_key = float(ts)
                    buckets = active_at_ts.setdefault(t_key, {})
                    buckets.setdefault((service, is_worker), set()).add(pod_name)

            if active_at_ts:
                snapshot.max_replicas_sandbox = max(
                    (len(b.get(("c-sandbox", False), set())) + len(b.get(("c-sandbox", True), set())) for b in active_at_ts.values()),
                    default=0,
                )
                snapshot.max_replicas_llm_worker = max(
                    (len(b.get(("llm-worker", False), set())) + len(b.get(("llm-worker", True), set())) for b in active_at_ts.values()),
                    default=0,
                )
                snapshot.max_replicas_core_api = max(
                    (len(b.get(("core-api", False), set())) + len(b.get(("core-api", True), set())) for b in active_at_ts.values()),
                    default=0,
                )
                snapshot.max_replicas_sandbox_worker = max(
                    (len(b.get(("c-sandbox", True), set())) for b in active_at_ts.values()),
                    default=0,
                )
                snapshot.max_replicas_llm_worker_worker = max(
                    (len(b.get(("llm-worker", True), set())) for b in active_at_ts.values()),
                    default=0,
                )
                snapshot.max_replicas_core_api_worker = max(
                    (len(b.get(("core-api", True), set())) for b in active_at_ts.values()),
                    default=0,
                )

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
