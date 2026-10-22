"""Run resumable, browser-driven SWCP benchmark inferences across local vLLM and Azure models.

Lifecycle per model (never two local models at once):
  configure + start the existing Compose service -> poll health -> 1 warmup submission
  -> full CodeNet suite through the frontend -> burst rounds -> stop vLLM, wait 5 s.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError, async_playwright

from dataset_loader import DEFAULT_FIXTURE_DIR, load_codenet, load_repo_fixtures
from frontend_driver import (
    ApiError,
    close_student_context,
    ensure_assignments,
    open_student_page,
    submit_from_frontend,
)
from leakage import leaks_reference
from pipeline_metrics import parse_pipeline_logs, summarize_submission


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
MODEL_ROOT = REPOSITORY_ROOT / "apps" / "localLLM" / "models"
LOG_DIR = REPOSITORY_ROOT / "apps" / "benchmark" / "logs"
AZURE_MODELS = ("Gpt6Sol", "Gpt4o")
# Matches download_models.py; Azure first so cloud runs proceed while weights download.
DEFAULT_MODELS = (
    "azure:Gpt6Sol",
    "azure:Gpt4o",
    "local:Qwen2.5-Coder-3B-Instruct-AWQ",
    "local:Qwen2.5-Coder-7B-Instruct-AWQ",
    "local:DeepSeek-R1-Distill-Qwen-7B-AWQ",
    "local:Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
    "local:Mistral-Nemo-Instruct-2407-12B-AWQ",
    "local:Qwen2.5-Coder-14B-Instruct-AWQ",
)
LOCAL_SERVICE = "local-llm-server"
SLA_MS = 15_000
EAGER_WEIGHT_BYTES = 7 * 1024**3
WARMUP_SAMPLE = {
    "id": "warmup",
    "slice": "warmup",
    "problem_id": None,
    "source_path": "warmup.c",
    "source": '#include <stdio.h>\n\nint main(void) {\n    printf("Hello, SWCP!\\n");\n    return 0;\n}\n',
    "problem_description": "Write a C program that prints Hello, SWCP! followed by a newline.",
}


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    name: str
    model_path: str | None = None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.name}"

    @property
    def frontend_backend(self) -> str:
        return "azure" if self.provider == "Azure" else "local"

    @property
    def served_name(self) -> str:
        return self.name.lower()


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key.strip()] = value
    return values


def env_value(name: str, dotenv: dict[str, str], *fallbacks: str, default: str = "") -> str:
    for key in (name, *fallbacks):
        value = os.environ.get(key) or dotenv.get(key)
        if value:
            return value
    return default


def parse_model(value: str) -> ModelSpec:
    provider, separator, name = value.partition(":")
    if not separator or not name.strip():
        raise ValueError(f"Model '{value}' must be formatted as azure:Deployment or local:directory.")
    provider = provider.strip().lower()
    name = name.strip()
    if provider == "azure":
        if name not in AZURE_MODELS:
            raise ValueError(f"Unsupported Azure deployment key '{name}'. Use {' or '.join(AZURE_MODELS)}.")
        return ModelSpec("Azure", name)
    if provider != "local":
        raise ValueError(f"Unsupported provider '{provider}' in model '{value}'.")
    model_path = name.replace("\\", "/").strip("/")
    if not model_path or any(part in {".", ".."} for part in model_path.split("/")):
        raise ValueError(f"Invalid local model path '{model_path}'.")
    return ModelSpec("vLLM", model_path.rsplit("/", 1)[-1], model_path=model_path)


def _model_dir(model: ModelSpec) -> Path:
    assert model.model_path is not None
    return MODEL_ROOT / Path(*model.model_path.split("/"))


def _weights_complete(path: Path) -> bool:
    if not (path / "config.json").is_file():
        return False
    cache = path / ".cache" / "huggingface" / "download"
    if cache.is_dir() and any(cache.rglob("*.incomplete")):
        return False
    index = path / "model.safetensors.index.json"
    if index.is_file():
        try:
            shards = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
        except (json.JSONDecodeError, KeyError, TypeError):
            return False
        return all((path / shard).is_file() for shard in shards)
    return (path / "model.safetensors").is_file()


def _wait_for_weights(model: ModelSpec, timeout_seconds: int) -> None:
    path = _model_dir(model)
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while not _weights_complete(path):
        if time.monotonic() > deadline:
            raise FileNotFoundError(
                f"Weights for {model.name} are incomplete at {path}. "
                "Run apps/benchmark/src/download_models.py; nothing is downloaded by the runner."
            )
        if not announced:
            print(f"Waiting for {model.name} weights to finish downloading...", flush=True)
            announced = True
        time.sleep(15)


def _gpu_memory_utilization(target: float = 0.92, reserve_mib: int = 320) -> str:
    """Use the requested 0.92 when possible, capped by currently free VRAM (the Windows desktop holds some)."""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        free_mib, total_mib = (int(part) for part in completed.stdout.splitlines()[0].split(","))
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return f"{target:.2f}"
    feasible = math.floor((free_mib - reserve_mib) / total_mib * 100) / 100
    return f"{max(0.5, min(target, feasible)):.2f}"


def _vllm_settings(model: ModelSpec) -> dict[str, str]:
    path = _model_dir(model)
    config = json.loads((path / "config.json").read_text(encoding="utf-8"))
    method = str((config.get("quantization_config") or {}).get("quant_method") or "").lower()
    weight_bytes = sum(item.stat().st_size for item in path.glob("*.safetensors"))
    large = weight_bytes >= EAGER_WEIGHT_BYTES
    return {
        "LLM_MODEL_PATH": model.model_path or "",
        "LLM_MODEL_NAME": model.served_name,
        "VLLM_QUANTIZATION": "awq_marlin" if method == "awq" else "none",
        "VLLM_KV_CACHE_DTYPE": "auto",
        "VLLM_GPU_MEMORY_UTILIZATION": _gpu_memory_utilization(),
        "VLLM_MAX_MODEL_LEN": "4096",
        "VLLM_MAX_NUM_SEQS": "4",
        "VLLM_ENFORCE_EAGER": "true" if large else "false",
    }


def _compose_env(dotenv: dict[str, str], teacher_username: str, teacher_password: str) -> dict[str, str]:
    env = os.environ.copy()
    env["POSTGRES_PORT"] = env_value("SWCP_BENCHMARK_POSTGRES_PORT", dotenv, default="15432")
    env["SEED_DEVELOPMENT_USERS"] = "true"
    env["DEV_TEACHER_USERNAME"] = teacher_username
    env["DEV_TEACHER_PASSWORD"] = teacher_password
    # Compose fails to interpolate the local-llm service unless these are always set.
    env.setdefault("LLM_MODEL_PATH", dotenv.get("LLM_MODEL_PATH") or "unset")
    env.setdefault("LLM_MODEL_NAME", dotenv.get("LLM_MODEL_NAME") or "unset")
    return env


def compose(args: list[str], env: dict[str, str], *, profiles: tuple[str, ...] = ("frontend",), timeout: int = 1800) -> str:
    command = ["docker", "compose"]
    for profile in profiles:
        command.extend(("--profile", profile))
    command.extend(args)
    completed = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        for key, value in env.items():
            if value and len(value) > 3 and any(
                marker in key.lower() for marker in ("password", "apikey", "api_key", "token", "secret")
            ):
                detail = detail.replace(value, "[redacted]")
        raise RuntimeError(f"docker compose {' '.join(args)} failed ({completed.returncode}): {detail[-2000:]}")
    return completed.stdout


def _http_ready(url: str, timeout_seconds: int = 300) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status < 500:
                    return
        except urllib.error.HTTPError as error:
            if error.code < 500:
                return
            last_error = str(error)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = str(error)
        time.sleep(2)
    raise TimeoutError(f"Service did not become ready at {url}: {last_error}")


def _wait_local_ready(local_url: str, env: dict[str, str], timeout_seconds: int = 1200) -> None:
    """Poll vLLM /health, failing fast if the container exits (e.g. CUDA OOM)."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{local_url.rstrip('/')}/health", timeout=3) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        state = compose(["ps", "-a", "--format", "{{.State}}", LOCAL_SERVICE], env, profiles=("local-llm",)).strip()
        logs = compose(["logs", "--no-color", "--tail", "300", LOCAL_SERVICE], env, profiles=("local-llm",))
        fatal = next((marker for marker in (
            "Engine core initialization failed", "CUDA out of memory", "OutOfMemoryError",
            "No available memory for the cache blocks",
        ) if marker in logs), None)
        if fatal or (state and state not in {"running", "created", "restarting"}):
            errors = [line for line in logs.splitlines() if "Error" in line and "File " not in line]
            raise RuntimeError(f"vLLM failed to start (state={state}, {fatal}): " + "\n".join(errors[-6:])[-3000:])
        time.sleep(5)
    raise TimeoutError(f"vLLM did not become healthy within {timeout_seconds}s.")


def _assert_local_model(url: str, served_name: str) -> None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/v1/models", timeout=10) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not inspect the ready local vLLM model: {error}") from error
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise RuntimeError("Local vLLM returned an invalid /v1/models response.")
    available = {item.get("id") for item in data if isinstance(item, dict)}
    if served_name not in available:
        raise RuntimeError(f"vLLM serves {sorted(map(str, available))}, not '{served_name}'.")


def _stop_local(env: dict[str, str]) -> None:
    compose(["stop", LOCAL_SERVICE], env, profiles=("local-llm",))
    time.sleep(5)


def _configure_model(model: ModelSpec, env: dict[str, str], frontend_url: str, local_url: str, args: argparse.Namespace) -> None:
    if model.provider == "Azure":
        if args.manage_stack:
            _stop_local(env)
            azure_env = env.copy()
            azure_env["AzureAIFoundry__DefaultModel"] = model.name
            compose(["up", "-d", "--force-recreate", "--no-deps", "llm-worker"], azure_env)
        else:
            configured = env_value("AzureAIFoundry__DefaultModel", read_dotenv(REPOSITORY_ROOT / ".env"), default="Gpt6Sol")
            if configured != model.name:
                raise RuntimeError(f"Stack management is disabled and the configured Azure model is '{configured}'.")
        _http_ready(frontend_url, 120)
        return

    _wait_for_weights(model, args.weights_timeout_seconds)
    if not args.manage_stack:
        _http_ready(f"{local_url.rstrip('/')}/health", 30)
        _assert_local_model(local_url, model.served_name)
        return
    local_env = env.copy()
    local_env.update(_vllm_settings(model))
    print(
        f"  vLLM settings: quantization={local_env['VLLM_QUANTIZATION']} "
        f"enforce_eager={local_env['VLLM_ENFORCE_EAGER']} gpu_util={local_env['VLLM_GPU_MEMORY_UTILIZATION']} "
        "max_len=4096 max_seqs=4",
        flush=True,
    )
    compose(["up", "-d", "--force-recreate", "--no-deps", LOCAL_SERVICE], local_env, profiles=("frontend", "local-llm"))
    _wait_local_ready(local_url, local_env)
    _assert_local_model(local_url, model.served_name)
    compose(["up", "-d", "--force-recreate", "--no-deps", "llm-worker"], local_env, profiles=("frontend", "local-llm"))
    _http_ready(frontend_url, 120)
    time.sleep(3)


def _load_pipeline_metrics(submission_id: str, started_at_utc: str) -> dict[str, Any]:
    since = datetime.fromisoformat(started_at_utc).astimezone(timezone.utc) - timedelta(seconds=2)
    since_arg = since.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    completed = subprocess.run(
        ["docker", "compose", "logs", "--no-color", "--timestamps", "--since", since_arg,
         "core-api", "c-sandbox-worker", "llm-worker"],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not retrieve pipeline logs (exit code {completed.returncode}).")
    events = parse_pipeline_logs(completed.stdout).get(submission_id, [])
    if not events:
        raise RuntimeError(f"No structured pipeline log events found for submission {submission_id}.")
    return summarize_submission(events)


def _estimated_cost(prompt_tokens: int | None, completion_tokens: int | None,
                    input_rate: float | None, output_rate: float | None) -> float | None:
    if None in (prompt_tokens, completion_tokens, input_rate, output_rate):
        return None
    return (prompt_tokens * input_rate + completion_tokens * output_rate) / 1_000_000  # type: ignore[operator]


def _load_pricing(path: Path | None) -> dict[str, dict[str, float]]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Pricing JSON must map model keys to input/output rates.")
    result: dict[str, dict[str, float]] = {}
    for model, rates in data.items():
        if str(model).startswith("_"):
            continue
        if not isinstance(rates, dict):
            raise ValueError(f"Pricing entry for '{model}' must be an object.")
        parsed: dict[str, float] = {}
        for name in ("input_usd_per_million", "output_usd_per_million"):
            value = rates.get(name)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"Pricing rate '{model}.{name}' must be numeric.")
            rate = float(value)
            if not math.isfinite(rate) or rate < 0:
                raise ValueError(f"Pricing rate '{model}.{name}' must be finite and nonnegative.")
            parsed[name] = rate
        result[str(model)] = parsed
    return result


async def _sample_vram(stop: asyncio.Event, values: list[int]) -> None:
    while not stop.is_set():
        try:
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
        except OSError:
            return
        if process.returncode == 0:
            for line in stdout.decode("ascii", errors="ignore").splitlines():
                if line.strip().isdigit():
                    values.append(int(line.strip()))
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except TimeoutError:
            pass


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        output.flush()
        os.fsync(output.fileno())


def _completed_run_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.is_file():
        return keys
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {error}") from error
        if record.get("status") == "COMPLETED" and record.get("run_key"):
            keys.add(str(record["run_key"]))
    return keys


def _run_key(sample: dict[str, Any], model: ModelSpec, context_mode: str = "frontend_submit") -> str:
    content = "\0".join(str(sample.get(key) or "") for key in ("source", "problem_description"))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    raw = f"{sample['id']}|{content_hash}|{model.key}|{model.model_path}|{context_mode}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _student_username(model: ModelSpec, slot: int) -> str:
    return "bench-" + hashlib.sha256(f"{model.key}|{slot}".encode("utf-8")).hexdigest()[:18]


def _sample_fields(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "sample_id": sample["id"],
        "slice": sample.get("slice"),
        "problem_id": sample.get("problem_id"),
        "codenet_status": sample.get("status"),
        "original_status": sample.get("original_status"),
        "source_path": sample.get("source_path"),
        "source_lines": sample.get("source_lines"),
        "reference_submission_id": sample.get("reference_submission_id"),
    }


def _failed_record(sample: dict[str, Any], model: ModelSpec, username: str, error: BaseException,
                   context_mode: str = "frontend_submit", **extra: Any) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "run_key": _run_key(sample, model, context_mode),
        **_sample_fields(sample),
        "provider": model.provider,
        "model_name": model.name,
        "context_mode": context_mode,
        "status": "FAILED",
        "error": f"{type(error).__name__}: {str(error).split(chr(10) + 'Call log:')[0][:1500]}",
        "frontend_username": username,
        "leaked_solution_code": False,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


async def _run_one(page: Any, sample: dict[str, Any], assignment_id: str, model: ModelSpec, username: str,
                   args: argparse.Namespace, context_mode: str = "frontend_submit", **extra: Any) -> dict[str, Any]:
    vram_values: list[int] = []
    stop_vram = asyncio.Event()
    vram_task = asyncio.create_task(_sample_vram(stop_vram, vram_values)) if model.provider == "vLLM" else None
    try:
        result = await submit_from_frontend(page, sample, assignment_id, model.frontend_backend, args.timeout_seconds)
    finally:
        if vram_task is not None:
            stop_vram.set()
            await vram_task

    metrics_error: str | None = None
    pipeline: dict[str, Any] = {"pipeline_breakdown_ms": {}, "prompt_tokens": None, "completion_tokens": None}
    try:
        pipeline = await asyncio.to_thread(_load_pipeline_metrics, result["platform_submission_id"], result["started_at_utc"])
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        metrics_error = str(error)

    breakdown = dict(pipeline["pipeline_breakdown_ms"])
    for key, value in result.get("websocket_phase_breakdown_ms", {}).items():
        if breakdown.get(key) is None:
            breakdown[key] = value
    prompt_tokens = pipeline.get("prompt_tokens")
    completion_tokens = pipeline.get("completion_tokens")
    llm_ms = breakdown.get("llm_inference")
    rates = args.pricing_by_model.get(model.key, {})
    input_rate = rates.get("input_usd_per_million", args.input_usd_per_million if model.provider == "Azure" else 0.0)
    output_rate = rates.get("output_usd_per_million", args.output_usd_per_million if model.provider == "Azure" else 0.0)
    e2e = result.get("e2e_total_latency_ms")
    feedback = str(result.get("feedback") or "")
    reference = str(sample.get("reference_source") or (sample.get("source") if sample.get("status") == "AC" else "") or "")
    return {
        "schema_version": 2,
        "run_key": _run_key(sample, model, context_mode),
        **_sample_fields(sample),
        "submission_id": result.get("platform_submission_id"),
        "assignment_id": assignment_id,
        "provider": model.provider,
        "model_name": model.name,
        "context_mode": context_mode,
        "status": result.get("status", "FAILED"),
        "frontend_network_ms": result.get("frontend_network_ms"),
        "frontend_ttft_ms": result.get("frontend_ttft_ms"),
        "e2e_total_latency_ms": e2e,
        "gateway_websocket_delivery_ms": result.get("gateway_websocket_delivery_ms"),
        "frontend_render_after_ws_ms": result.get("frontend_render_after_ws_ms"),
        "pipeline_breakdown_ms": breakdown,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tokens_per_sec": round(completion_tokens * 1000 / llm_ms, 3) if completion_tokens and llm_ms else None,
        "estimated_cost_usd": _estimated_cost(prompt_tokens, completion_tokens, input_rate, output_rate),
        "peak_vram_mb": max(vram_values) if vram_values else None,
        "exceeded_15s_sla": e2e is not None and e2e > SLA_MS,
        "leaked_solution_code": leaks_reference(feedback, reference) if reference else False,
        "feedback": feedback,
        "task_solved": result.get("task_solved"),
        "sandbox_output": result.get("sandbox_output"),
        "events": result.get("events", []),
        "started_at_utc": result.get("started_at_utc"),
        "completed_at_utc": result.get("completed_at_utc"),
        "telemetry_error": metrics_error,
        "error": result.get("error") or result.get("platform_error"),
        "frontend_username": username,
        **extra,
    }


async def _safe_run(page: Any, sample: dict[str, Any], assignment_id: str, model: ModelSpec, username: str,
                    args: argparse.Namespace, context_mode: str = "frontend_submit", **extra: Any) -> dict[str, Any]:
    try:
        return await _run_one(page, sample, assignment_id, model, username, args, context_mode, **extra)
    except (ApiError, PlaywrightError, RuntimeError, TimeoutError, OSError, KeyError) as error:
        return _failed_record(sample, model, username, error, context_mode, **extra)


def _load_samples(args: argparse.Namespace, dotenv: dict[str, str]) -> list[dict[str, Any]]:
    cache = args.dataset_cache
    if cache.is_file():
        samples = [json.loads(line) for line in cache.read_text(encoding="utf-8").splitlines() if line.strip()]
        meta = samples[0].get("_cache_meta") if samples else None
        if meta == {"samples_per_status": args.samples_per_status, "fixtures": args.include_repo_fixtures}:
            print(f"Loaded {len(samples) - 1} cached samples from {cache}", flush=True)
            return samples[1:]
    archive = args.archive or env_value("SWCP_CODENET_ARCHIVE", dotenv)
    if not archive:
        raise ValueError("Pass --archive or set SWCP_CODENET_ARCHIVE to the local CodeNet tar.gz.")
    print(f"Sampling CodeNet from {archive} (streams the archive; may take several minutes)...", flush=True)
    samples = load_codenet(archive, args.samples_per_status)
    if args.include_repo_fixtures:
        samples.extend(load_repo_fixtures(args.fixtures_dir))
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.open("w", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps({"_cache_meta": {"samples_per_status": args.samples_per_status,
                                                 "fixtures": args.include_repo_fixtures}}) + "\n")
        for sample in samples:
            output.write(json.dumps(sample, ensure_ascii=False) + "\n")
    print(f"Sampled {len(samples)} records; cached at {cache}", flush=True)
    return samples


async def _run_burst(browser: Any, model: ModelSpec, samples: list[dict[str, Any]], assignment_ids: dict[str, str],
                     frontend_url: str, password: str, args: argparse.Namespace, burst_log: Path,
                     done: set[str]) -> None:
    pool = [sample for sample in samples if sample.get("slice") == "codenet"] or samples
    for level in args.burst_levels:
        mode = f"burst_n{level}"
        batch = [pool[index % len(pool)] for index in range(level)]
        keys = [_run_key({**sample, "id": f"{sample['id']}#slot{slot}"}, model, mode) for slot, sample in enumerate(batch)]
        if all(key in done for key in keys):
            continue
        print(f"  Burst N={level}", flush=True)
        contexts: list[tuple[Any, Any, str]] = []
        try:
            for slot in range(level):
                username = _student_username(model, 100 + slot)
                context, page = await open_student_page(browser, frontend_url, username, password)
                contexts.append((context, page, username))
                await page.get_by_text(_title_for(batch[slot]), exact=True).first.wait_for(timeout=30_000)
            records = await asyncio.gather(*(
                _safe_run(page, {**batch[slot], "id": f"{batch[slot]['id']}#slot{slot}"},
                          assignment_ids[str(batch[slot]["id"])], model, username, args, mode, burst_level=level)
                for slot, (context, page, username) in enumerate(contexts)
            ))
        finally:
            for context, _, _ in contexts:
                await close_student_context(context)
        for record in records:
            _append_jsonl(burst_log, record)
            if record["status"] == "COMPLETED":
                done.add(record["run_key"])
        latencies = [r["e2e_total_latency_ms"] for r in records if r.get("e2e_total_latency_ms")]
        ok = sum(r["status"] == "COMPLETED" for r in records)
        p95 = sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None
        print(f"    {ok}/{level} completed, p95={p95} ms", flush=True)


def _title_for(sample: dict[str, Any]) -> str:
    from frontend_driver import assignment_title
    return assignment_title(sample)


async def run(args: argparse.Namespace) -> None:
    dotenv = read_dotenv(REPOSITORY_ROOT / ".env")
    teacher_username = env_value("SWCP_BENCHMARK_TEACHER_USERNAME", dotenv, "DEV_TEACHER_USERNAME")
    teacher_password = env_value("SWCP_BENCHMARK_TEACHER_PASSWORD", dotenv, "DEV_TEACHER_PASSWORD")
    student_password = env_value("SWCP_BENCHMARK_STUDENT_PASSWORD", dotenv)
    if len(student_password) < 5:
        raise ValueError("Set SWCP_BENCHMARK_STUDENT_PASSWORD (at least 5 characters) in .env.")
    if not teacher_username or not teacher_password:
        raise ValueError("Set SWCP_BENCHMARK_TEACHER_USERNAME and SWCP_BENCHMARK_TEACHER_PASSWORD in .env.")

    models = [parse_model(value) for value in (args.models or DEFAULT_MODELS)]
    args.pricing_by_model = _load_pricing(args.pricing_json)
    samples = _load_samples(args, dotenv)
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        raise ValueError("No benchmark samples were selected.")

    output = args.output.resolve()
    burst_log = args.burst_output.resolve()
    completed_keys = _completed_run_keys(output)
    burst_done = _completed_run_keys(burst_log)

    frontend_url = args.frontend_url or f"http://localhost:{env_value('FRONTEND_PORT', dotenv, default='5174')}"
    local_url = f"http://localhost:{env_value('LOCAL_LLM_PORT', dotenv, default='8080')}"
    env = _compose_env(dotenv, teacher_username, teacher_password)
    if args.manage_stack:
        print("Building and starting the existing Compose stack (frontend profile)...", flush=True)
        compose(["up", "-d", "--build", "api", "core-api", "llm-worker", "c-sandbox-worker", "frontend"], env)
        _http_ready(frontend_url, 600)
        _http_ready(f"http://localhost:{env_value('API_PORT', dotenv, default='5170')}/api/assignments/", 300)

    assignment_ids = await asyncio.to_thread(
        ensure_assignments, frontend_url, teacher_username, teacher_password, [*samples, WARMUP_SAMPLE]
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for model in models:
                pending = [s for s in samples if _run_key(s, model) not in completed_keys]
                burst_pending = bool(args.burst_levels)
                if not pending and not burst_pending:
                    continue
                print(f"\n=== {model.key}: {len(pending)} pending samples ===", flush=True)
                try:
                    await asyncio.to_thread(_configure_model, model, env, frontend_url, local_url, args)
                except (RuntimeError, TimeoutError, FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
                    print(f"  SKIPPING {model.key}: {error}", flush=True)
                    _append_jsonl(output.with_name("model_failures.jsonl"), {
                        "provider": model.provider, "model_name": model.name,
                        "error": f"{type(error).__name__}: {error}"[-4000:],
                        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                    })
                    if model.provider == "vLLM" and args.manage_stack:
                        await asyncio.to_thread(_stop_local, env)
                    continue

                username = _student_username(model, 0)
                context, page = await open_student_page(browser, frontend_url, username, student_password)
                try:
                    warm = await _safe_run(page, WARMUP_SAMPLE, assignment_ids["warmup"], model, username, args, "warmup")
                    print(f"  warmup: {warm['status']} {warm.get('e2e_total_latency_ms')} ms {warm.get('error') or ''}", flush=True)
                    latencies: list[float] = []
                    for index, sample in enumerate(pending, 1):
                        record = await _safe_run(page, sample, assignment_ids[str(sample["id"])], model, username, args)
                        _append_jsonl(output, record)
                        if record["status"] == "COMPLETED":
                            completed_keys.add(record["run_key"])
                        if record.get("e2e_total_latency_ms"):
                            latencies.append(record["e2e_total_latency_ms"])
                        suffix = f" error={record['error']}" if record.get("error") and record["status"] != "COMPLETED" else ""
                        print(f"  [{index}/{len(pending)}] {sample['id']} {record['status']} "
                              f"{record.get('e2e_total_latency_ms')} ms{suffix}", flush=True)
                        if record["status"] != "COMPLETED" and "Target page, context or browser has been closed" in str(record.get("error")):
                            await close_student_context(context)
                            context, page = await open_student_page(browser, frontend_url, username, student_password)
                    if latencies:
                        print(f"  median e2e {statistics.median(latencies):.0f} ms", flush=True)
                finally:
                    await close_student_context(context)

                if args.burst_levels:
                    await _run_burst(browser, model, samples, assignment_ids, frontend_url, student_password,
                                     args, burst_log, burst_done)
                if model.provider == "vLLM" and args.manage_stack:
                    await asyncio.to_thread(_stop_local, env)
                print(f"=== Finished {model.key} ===", flush=True)
        finally:
            await browser.close()
    print(f"\nInference log: {output}\nBurst log: {burst_log}", flush=True)


def _levels(value: str) -> list[int]:
    if value.strip().lower() in {"", "none", "0"}:
        return []
    levels = [int(part) for part in value.split(",") if part.strip()]
    if any(level < 1 for level in levels):
        raise argparse.ArgumentTypeError("Burst levels must be positive integers.")
    return levels


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--archive", type=Path, help="Local Project_CodeNet.tar.gz (default: SWCP_CODENET_ARCHIVE).")
    parser.add_argument("--samples-per-status", type=int, default=20)
    parser.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--no-repo-fixtures", dest="include_repo_fixtures", action="store_false")
    parser.add_argument("--dataset-cache", type=Path, default=LOG_DIR / "dataset.jsonl")
    parser.add_argument("--limit", type=int, default=0, help="Only run the first N samples (smoke tests).")
    parser.add_argument("--model", dest="models", action="append",
                        help="Repeatable: azure:Gpt6Sol, azure:Gpt4o, local:<dir under apps/localLLM/models>.")
    parser.add_argument("--output", type=Path, default=LOG_DIR / "inference_logs.jsonl")
    parser.add_argument("--burst-output", type=Path, default=LOG_DIR / "burst_logs.jsonl")
    parser.add_argument("--burst-levels", type=_levels, default=[1, 5, 10, 20],
                        help="Comma-separated concurrent submission levels, or 'none'.")
    parser.add_argument("--frontend-url")
    parser.add_argument("--timeout-seconds", type=int, default=420)
    parser.add_argument("--weights-timeout-seconds", type=int, default=6 * 3600)
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    parser.add_argument("--pricing-json", type=Path,
                        default=REPOSITORY_ROOT / "apps" / "benchmark" / "pricing.json")
    parser.add_argument("--no-stack-management", dest="manage_stack", action="store_false")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.samples_per_status < 0 or args.timeout_seconds < 1 or args.limit < 0:
        parser.error("Counts must be nonnegative and the timeout positive.")
    if args.pricing_json and not args.pricing_json.is_file():
        args.pricing_json = None
    try:
        asyncio.run(run(args))
    except (ApiError, FileNotFoundError, PlaywrightError, RuntimeError, TimeoutError, OSError, ValueError) as error:
        print(f"Benchmark failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
