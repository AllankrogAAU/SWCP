"""Per-submission Loki/Tempo collection and one-second NVIDIA GPU sampling."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from apps.benchmark.artifacts import JsonlWriter
from benchmarks.config import BenchmarkConfig
from benchmarks.telemetry import resolve_loki_url, resolve_prometheus_url


class GpuSampler:
    def __init__(self, interval_seconds: float = 1.0) -> None:
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self, writer: JsonlWriter, model_key: str) -> None:
        if self._task is not None:
            raise RuntimeError("GPU sampler is already running.")
        self._stop.clear()
        self._task = asyncio.create_task(self._run(writer, model_key))

    async def _run(self, writer: JsonlWriter, model_key: str) -> None:
        while not self._stop.is_set():
            sampled_at = datetime.now(timezone.utc).isoformat()
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,power.draw",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError(f"nvidia-smi failed: {stderr.decode(errors='replace').strip()}")
            lines = stdout.decode("utf-8").splitlines()
            if not lines:
                raise RuntimeError("nvidia-smi returned no GPU samples.")
            for line in lines:
                parts = [part.strip() for part in line.split(",")]
                if len(parts) < 5:
                    raise RuntimeError(f"Unexpected nvidia-smi sample: {line}")
                try:
                    power_watts = float(parts[5]) if len(parts) > 5 else None
                except ValueError:
                    power_watts = None
                record = {
                    "record_type": "gpu_sample",
                    "model_key": model_key,
                    "timestamp_utc": sampled_at,
                    "gpu_index": int(parts[0]),
                    "gpu_name": parts[1],
                    "memory_used_mb": int(parts[2]),
                    "memory_total_mb": int(parts[3]),
                    "utilization_percent": int(parts[4]),
                    "power_watts": power_watts,
                }
                self.samples.append(record)
                writer.write(record)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> list[dict[str, Any]]:
        if self._task is None:
            return self.samples
        self._stop.set()
        task, self._task = self._task, None
        await task
        return self.samples


class TelemetryCollector:
    PROMETHEUS_QUERIES = {
        "vllm_kv_cache_usage": 'vllm:kv_cache_usage_perc{namespace="swcp"}',
        "vllm_generation_tokens_per_second": 'rate(vllm:generation_tokens_total{namespace="swcp"}[30s])',
        "container_memory_bytes": 'container_memory_working_set_bytes{namespace="swcp",container!="",container!="POD"}',
        "container_cpu_cores": 'rate(container_cpu_usage_seconds_total{namespace="swcp",container!="",container!="POD"}[30s])',
    }

    def __init__(self, config: BenchmarkConfig) -> None:
        self.prometheus_url = resolve_prometheus_url(config.prometheus_url)
        self.loki_url = resolve_loki_url(config.loki_url)
        self.tempo_url = os.getenv("TEMPO_URL", "http://tempo.monitoring.svc.cluster.local:3200").rstrip("/")

    @staticmethod
    def _get_json(url: str, timeout_seconds: float = 15.0) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
        # OSError covers URLError plus connection resets/refusals; HTTPException covers truncated replies.
        except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Telemetry request failed for {url}: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Telemetry endpoint returned non-object JSON: {url}")
        return data

    def collect_submission(
        self,
        submission_id: str,
        start_epoch: float,
        end_epoch: float,
    ) -> dict[str, Any]:
        start_ns = int((start_epoch - 10) * 1_000_000_000)
        end_ns = int((end_epoch + 15) * 1_000_000_000)
        params = urllib.parse.urlencode({
            "query": f'{{namespace="swcp"}} |= "{submission_id}"',
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": "200",
            "direction": "FORWARD",
        })
        result: dict[str, Any] = {
            "available": False,
            "log_entries": 0,
            "malformed_log_entries": 0,
            "stage_timings_ms": {},
            "trace_id": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "tokens_per_second": None,
            "outcome": None,
            "error": None,
        }
        try:
            data = self._get_json(f"{self.loki_url}/loki/api/v1/query_range?{params}")
        except RuntimeError as exc:
            result["error"] = str(exc)
            return result
        streams = data.get("data", {}).get("result", [])
        if not isinstance(streams, list):
            result["error"] = "Loki returned an invalid result list."
            return result

        for stream in streams:
            for entry in stream.get("values", []):
                if not isinstance(entry, list) or len(entry) < 2:
                    result["malformed_log_entries"] += 1
                    continue
                result["log_entries"] += 1
                line = str(entry[1])
                json_start = line.find("{")
                if json_start < 0:
                    result["malformed_log_entries"] += 1
                    continue
                try:
                    payload = json.loads(line[json_start:])
                except json.JSONDecodeError:
                    result["malformed_log_entries"] += 1
                    continue
                state = payload.get("State") or payload
                if not isinstance(state, dict):
                    result["malformed_log_entries"] += 1
                    continue
                event = str(state.get("event_name", ""))
                stage = str(state.get("stage", ""))
                if state.get("trace_id"):
                    result["trace_id"] = str(state["trace_id"])
                if event == "pipeline.stage.completed" and state.get("duration_ms") is not None:
                    result["stage_timings_ms"][stage] = float(state["duration_ms"])
                if event == "pipeline.stage.completed" and stage == "llm":
                    result["prompt_tokens"] = state.get("prompt_tokens")
                    result["completion_tokens"] = state.get("completion_tokens")
                    result["tokens_per_second"] = state.get("tokens_per_second")
                if event == "pipeline.completed":
                    result["outcome"] = state.get("outcome")
        result["available"] = result["log_entries"] > 0
        if not result["available"]:
            result["error"] = "Loki returned no matching submission logs."

        trace_id = result["trace_id"]
        if trace_id:
            try:
                trace = self._get_json(f"{self.tempo_url}/api/traces/{trace_id}")
                batches = trace.get("batches", [])
                result["tempo"] = {
                    "available": bool(batches),
                    "batch_count": len(batches) if isinstance(batches, list) else 0,
                }
                if not batches:
                    result["tempo"]["error"] = "Tempo returned no spans for the trace id."
            except RuntimeError as exc:
                result["tempo"] = {"available": False, "error": str(exc)}
        else:
            result["tempo"] = {"available": False, "error": "No trace id was found in Loki logs."}
        return result

    def collect_prometheus(
        self,
        start_epoch: float,
        end_epoch: float,
        step_seconds: int = 15,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "url": self.prometheus_url,
            "step_seconds": step_seconds,
            "queries": {},
        }
        for name, query in self.PROMETHEUS_QUERIES.items():
            params = urllib.parse.urlencode({
                "query": query,
                "start": str(start_epoch),
                "end": str(end_epoch),
                "step": f"{step_seconds}s",
            })
            try:
                data = self._get_json(f"{self.prometheus_url}/api/v1/query_range?{params}")
                if data.get("status") != "success":
                    raise RuntimeError(f"Prometheus returned status {data.get('status')!r}.")
                series = data.get("data", {}).get("result")
                if not isinstance(series, list):
                    raise RuntimeError("Prometheus returned an invalid time-series result.")
                result["queries"][name] = {"query": query, "series": series, "error": None}
            except RuntimeError as exc:
                result["queries"][name] = {"query": query, "series": [], "error": str(exc)}
        return result


def summarize_gpu_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {"available": False, "error": "No GPU samples were collected."}
    memory = [int(sample["memory_used_mb"]) for sample in samples]
    utilization = [int(sample["utilization_percent"]) for sample in samples]
    power = [float(sample["power_watts"]) for sample in samples if sample.get("power_watts") is not None]
    return {
        "available": True,
        "sample_count": len(samples),
        "gpu_name": samples[-1]["gpu_name"],
        "memory_peak_mb": max(memory),
        "memory_mean_mb": sum(memory) / len(memory),
        "utilization_peak_percent": max(utilization),
        "utilization_mean_percent": sum(utilization) / len(utilization),
        "power_mean_watts": sum(power) / len(power) if power else None,
        "power_peak_watts": max(power) if power else None,
    }
