"""Kubernetes-backed CodeNet inference, QoS collection, judging, and publication artifacts."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import uuid
from typing import Any

import httpx

from apps.benchmark.artifacts import JsonlWriter
from apps.benchmark.assignments import AssignmentProvisioner, resolve_teacher_token
from apps.benchmark.dataset import DEFAULT_STATUSES, RUBRIC_STATUSES, CodeNetSample, load_archive_samples
from apps.benchmark.judge import add_judge_arguments, judge_run, judge_sample, resolve_judge_config
from apps.benchmark.model_manager import (
    DEFAULT_CLOUD_MODEL,
    MODELS,
    LocalModelManager,
    ModelSpec,
    installed_local_models,
)
from apps.benchmark.plots import write_summary
from apps.benchmark.preflight import DEFAULT_TOKEN_BUDGET, estimate_context_prompt_tokens
from apps.benchmark.progress import ProgressTracker
from apps.benchmark.telemetry import GpuSampler, TelemetryCollector, summarize_gpu_samples
from benchmarks.config import BenchmarkConfig
from benchmarks.runner import ClientSubmissionResult, LoadRunner
from benchmarks.seeder import Seeder
from benchmarks.workloads import get_workload_source

LOGGER = logging.getLogger("apps.benchmark")
FIXTURES = ("pass", "compile_error", "runtime_error", "infinite_loop")
FIXTURE_STATUS = {"pass": "AC", "compile_error": "CE", "runtime_error": "RE", "infinite_loop": "TLE"}
DEFAULT_PROGRESS_FILE = Path(__file__).resolve().parent / "LIVE_PROGRESS.md"


@dataclass
class RunControls:
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    warmup_requests: int = 2
    progress: ProgressTracker | None = None


def _git_value(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


# Architecturally diverse default matrix (Qwen, Llama, Gemma, Mistral, R1-distill reasoning, Qwen scale ablation),
# run serially on one GPU and closed by the Azure cloud baseline.
DEFAULT_MODEL_MATRIX = (
    "qwen2.5-coder-7b",
    "meta-llama-3.1-8b",
    "gemma-2-9b",
    "mistral-nemo-12b",
    "deepseek-r1-distill-llama-8b",
    "qwen2.5-coder-14b",
    "qwen2.5-coder-3b",
    DEFAULT_CLOUD_MODEL,
)


def _model_list(values: list[str] | None) -> list[ModelSpec]:
    """Requested models, or the installed models of the default matrix followed by the GPT-6-sol baseline."""
    installed = {spec.key for spec in installed_local_models()}
    keys = values or [
        key for key in DEFAULT_MODEL_MATRIX if MODELS[key].backend != "local" or key in installed
    ]
    unknown = set(keys) - set(MODELS)
    if unknown:
        raise ValueError(f"Unknown models: {', '.join(sorted(unknown))}")
    return [MODELS[key] for key in dict.fromkeys(keys)]


def _source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _sample_metadata(sample: CodeNetSample) -> dict[str, Any]:
    return {
        "problem_id": sample.problem_id,
        "problem_title": sample.title,
        "codenet_status": sample.status,
        "codenet_submission_id": sample.submission_id,
        "accepted_submission_id": sample.accepted_submission_id,
        "source_sha256": _source_hash(sample.read_source()),
        "accepted_source_sha256": _source_hash(sample.read_accepted_source()),
        "timeout_ms": sample.timeout_ms,
        "memory_limit_mb": sample.memory_limit_mb,
    }


def _select_burst_samples(samples: list[CodeNetSample], burst_size: int) -> list[CodeNetSample]:
    if burst_size <= 0:
        return []
    selected: list[CodeNetSample] = []
    seen_statuses: set[str] = set()
    for sample in samples:
        if sample.status in seen_statuses:
            continue
        selected.append(sample)
        seen_statuses.add(sample.status)
        if len(selected) == burst_size:
            break
    return selected


async def _write_submission_record(
    writer: JsonlWriter,
    telemetry: TelemetryCollector,
    runner: LoadRunner,
    client: httpx.AsyncClient,
    model: ModelSpec,
    context_mode: str,
    source: str,
    assignment_id: str,
    step_label: str,
    fixture_type: str,
    expected_status: str,
    timeout_ms: int,
    memory_limit_mb: int,
    metadata: dict[str, Any],
    controls: RunControls | None = None,
) -> ClientSubmissionResult:
    controls = controls or RunControls()
    attempts: list[dict[str, Any]] = []
    for attempt in range(1, max(1, controls.max_attempts) + 1):
        started_epoch = time.time()
        started_at = datetime.now(timezone.utc).isoformat()
        result = await runner.execute_single_submission(
            step_label,
            client,
            archetype=fixture_type,
            timeout_s=240.0,
            source_code=source,
            assignment_id=assignment_id,
            llm_backend=model.backend,
            context_mode=context_mode,
            azure_model_key=model.azure_model_key,
        )
        ended_epoch = time.time()
        if result.final_status == "COMPLETED" or attempt == max(1, controls.max_attempts):
            break
        attempts.append({
            "attempt": attempt,
            "submission_id": result.submission_id,
            "final_status": result.final_status,
            "terminal_via": result.terminal_via,
            "error_message": result.error_message,
        })
        delay = controls.retry_backoff_seconds * 2 ** (attempt - 1)
        LOGGER.warning(
            "%s %s attempt %d ended %s (%s); retrying in %.0fs.",
            model.key, metadata.get("codenet_submission_id") or fixture_type, attempt,
            result.final_status, result.error_message, delay,
        )
        await asyncio.sleep(delay)
    judge = judge_sample(
        expected_status,
        result.sandbox_output,
        timeout_ms,
        memory_limit_mb,
        result.task_solved,
        context_mode,
    )
    final_feedback = result.llm_feedback
    record = {
        "record_type": "submission",
        "submission_id": result.submission_id,
        "started_at_utc": started_at,
        "ended_at_utc": datetime.now(timezone.utc).isoformat(),
        "model_key": model.key,
        "model_name": model.display_name,
        "backend": model.backend,
        "azure_model_key": model.azure_model_key,
        "context_mode": context_mode,
        "step_label": step_label,
        "traffic_mode": (
            "burst" if step_label.startswith("burst_")
            else "fixture" if fixture_type.startswith("fixture_")
            else "sequential"
        ),
        "fixture_type": fixture_type,
        **metadata,
        "source_sha256": metadata.get("source_sha256", _source_hash(source)),
        "final_status": result.final_status,
        "terminal_via": result.terminal_via,
        "client_e2e_ms": result.client_e2e_ms,
        "http_accepted_ms": result.http_accepted_ms,
        "frontend_ttft_ms": result.frontend_ttft_ms,
        "frontend_final_chunk_ms": result.frontend_final_chunk_ms,
        "ws_events_count": result.ws_events_count,
        "streamed_feedback": result.streamed_feedback,
        "llm_feedback": final_feedback,
        "streamed_feedback_matches_final": (
            result.streamed_feedback == final_feedback if final_feedback is not None else None
        ),
        "task_solved": result.task_solved,
        "error_message": result.error_message,
        "response_collection_error": result.response_collection_error,
        "sandbox_output": result.sandbox_output,
        "judge": judge,
        "telemetry": {"collection_status": "pending"},
        "attempt_count": len(attempts) + 1,
        "failed_attempts": attempts,
    }
    writer.write(record)
    if controls.progress is not None:
        controls.progress.record(result.final_status == "COMPLETED", result.submission_id)
    telemetry_data = await asyncio.to_thread(
        telemetry.collect_submission,
        result.submission_id,
        started_epoch,
        ended_epoch,
    )
    writer.write({
        "record_type": "telemetry_update",
        "submission_id": result.submission_id,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(),
        "telemetry": telemetry_data,
    })
    return result


async def _run_model(
    model: ModelSpec,
    samples: list[CodeNetSample],
    assignments: dict[str, str],
    fixture_assignment_id: str | None,
    tokens: list[str],
    config: BenchmarkConfig,
    run_id: str,
    writer: JsonlWriter,
    telemetry: TelemetryCollector,
    context_modes: list[str],
    burst_size: int,
    burst_window_seconds: float,
    controls: RunControls | None = None,
) -> dict[str, Any]:
    controls = controls or RunControls()
    runner = LoadRunner(config, tokens, next(iter(assignments.values())), run_id)
    warmups = await _run_warmups(runner, config, model, samples, assignments, context_modes, controls)
    gpu_sampler = GpuSampler()
    model_start = time.time()
    gpu_sampler.start(writer, model.key)
    try:
        async with httpx.AsyncClient(base_url=config.target_url, timeout=30.0) as client:
            for context_mode in context_modes:
                for sample in samples:
                    await _write_submission_record(
                        writer,
                        telemetry,
                        runner,
                        client,
                        model,
                        context_mode,
                        sample.read_source(),
                        assignments[sample.problem_id],
                        f"codenet_{context_mode}",
                        f"codenet_{sample.status}",
                        sample.status,
                        sample.timeout_ms,
                        sample.memory_limit_mb,
                        _sample_metadata(sample),
                        controls,
                    )

            if burst_size:
                burst_samples = _select_burst_samples(samples, burst_size)
                started = time.monotonic()
                burst_tasks: list[asyncio.Task[ClientSubmissionResult]] = []
                interval = burst_window_seconds / max(1, len(burst_samples))
                burst_context_mode = (
                    "with_context_transfer"
                    if "with_context_transfer" in context_modes
                    else context_modes[0]
                )
                for index, sample in enumerate(burst_samples):
                    task = asyncio.create_task(_write_submission_record(
                        writer,
                        telemetry,
                        runner,
                        client,
                        model,
                        burst_context_mode,
                        sample.read_source(),
                        assignments[sample.problem_id],
                        f"burst_{len(burst_samples)}",
                        "burst",
                        sample.status,
                        sample.timeout_ms,
                        sample.memory_limit_mb,
                        _sample_metadata(sample),
                        controls,
                    ))
                    burst_tasks.append(task)
                    if index + 1 < len(burst_samples):
                        await asyncio.sleep(interval)
                await asyncio.gather(*burst_tasks)
                LOGGER.info(
                    "Model %s burst of %d completed in %.1fs.",
                    model.key,
                    len(burst_samples),
                    time.monotonic() - started,
                )

            if fixture_assignment_id:
                for context_mode in context_modes:
                    for fixture in FIXTURES:
                        source = get_workload_source(fixture, run_id, 0)
                        await _write_submission_record(
                            writer,
                            telemetry,
                            runner,
                            client,
                            model,
                            context_mode,
                            source,
                            fixture_assignment_id,
                            f"fixtures_{context_mode}",
                            f"fixture_{fixture}",
                            FIXTURE_STATUS[fixture],
                            2_000,
                            128,
                            {
                                "codenet_status": "FIXTURE",
                                "fixture_expected_status": FIXTURE_STATUS[fixture],
                            },
                            controls,
                        )
    finally:
        gpu_samples = await gpu_sampler.stop()
    model_end = time.time()
    return {
        "model_key": model.key,
        "started_at_epoch": model_start,
        "ended_at_epoch": model_end,
        "duration_seconds": model_end - model_start,
        "warmups": warmups,
        "gpu": summarize_gpu_samples(gpu_samples),
        "prometheus": telemetry.collect_prometheus(model_start, model_end),
    }


async def _run_warmups(
    runner: LoadRunner,
    config: BenchmarkConfig,
    model: ModelSpec,
    samples: list[CodeNetSample],
    assignments: dict[str, str],
    context_modes: list[str],
    controls: RunControls,
) -> list[dict[str, Any]]:
    """Unrecorded end-to-end requests that warm vLLM/worker caches after a model swap."""
    if controls.progress is not None:
        controls.progress.set_phase("warmup", model.key, f"Warmup ({controls.warmup_requests}) for {model.key}")
    warmups: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=config.target_url, timeout=30.0) as client:
        for index in range(max(0, controls.warmup_requests)):
            sample = samples[index % len(samples)]
            result = await runner.execute_single_submission(
                "warmup",
                client,
                archetype="warmup",
                timeout_s=300.0,
                source_code=sample.read_source(),
                assignment_id=assignments[sample.problem_id],
                llm_backend=model.backend,
                context_mode=context_modes[0],
                azure_model_key=model.azure_model_key,
            )
            warmups.append({
                "submission_id": result.submission_id,
                "final_status": result.final_status,
                "client_e2e_ms": result.client_e2e_ms,
            })
            LOGGER.info("Warmup %d for %s: %s in %.0f ms.", index + 1, model.key, result.final_status, result.client_e2e_ms)
    if controls.progress is not None:
        controls.progress.set_phase("benchmark", model.key, f"Recording {model.key}")
    return warmups


def _write_run_summary(path: Path, run_id: str, run_data: dict[str, Any]) -> None:
    path.write_text(json.dumps({"run_id": run_id, **run_data}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sandbox_stage(sandbox_output: dict[str, Any] | None) -> str:
    if not sandbox_output:
        return "UNKNOWN"
    compilation = sandbox_output.get("compilation")
    if not isinstance(compilation, dict):
        return "UNKNOWN"
    classification = str(sandbox_output.get("errorClassification") or "").lower()
    if compilation.get("success") is False:
        return "CE" if classification == "compile_error" else "COMPILE_TIMEOUT"

    if classification == "run_timeout":
        return "TLE"
    if classification == "memory_limit_exceeded":
        return "MLE"
    if classification == "runtime_error":
        return "RE"
    if classification == "wrong_answer":
        return "WA"
    if classification == "success":
        test_results = sandbox_output.get("testResults")
        if isinstance(test_results, list) and test_results and all(
            isinstance(test, dict) and test.get("passed") is True
            for test in test_results
        ):
            return "AC"
    return "UNKNOWN"


def _sandbox_evidence(sandbox_output: dict[str, Any] | None) -> dict[str, Any] | None:
    if not sandbox_output:
        return None
    compilation = sandbox_output.get("compilation")
    test_results = sandbox_output.get("testResults")
    return {
        "compilation": {
            "success": compilation.get("success"),
            "exitCode": compilation.get("exitCode"),
        } if isinstance(compilation, dict) else None,
        "errorClassification": sandbox_output.get("errorClassification"),
        "testResults": [
            {
                "passed": test.get("passed"),
                "exitCode": test.get("exitCode"),
                "durationMilliseconds": test.get("durationMilliseconds"),
                "memoryConsumedMegabytes": test.get("memoryConsumedMegabytes"),
                "stderr": (test.get("stderr") or "")[:1_000],
            }
            for test in test_results
            if isinstance(test, dict)
        ] if isinstance(test_results, list) else [],
    }


async def _validate_candidate_pool(
    candidate_pool: list[CodeNetSample],
    assignments: dict[str, str],
    tokens: list[str],
    config: BenchmarkConfig,
    output_path: Path,
    run_id: str,
    per_status: int,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    max_attempts: int = 3,
    retry_backoff_seconds: float = 5.0,
) -> tuple[list[CodeNetSample], list[dict[str, Any]]]:
    """Keep candidates whose sandbox stage matches the CodeNet label and whose
    with-context-transfer prompt fits the token budget; unsuitable candidates are swapped
    for the next unaltered candidate of the same status."""
    if not assignments:
        raise RuntimeError("Cannot validate CodeNet samples without provisioned assignments.")
    runner = LoadRunner(config, tokens, next(iter(assignments.values())), run_id)
    records: list[dict[str, Any]] = []
    selected: dict[str, list[CodeNetSample]] = {}
    accepted_cache: dict[
        tuple[str, str],
        tuple[str, str | None, dict[str, Any] | None, dict[str, Any] | None],
    ] = {}
    persisted_record_count = 0

    def persist(force: bool = False) -> None:
        nonlocal persisted_record_count
        if not force and len(records) - persisted_record_count < 25:
            return
        _write_run_summary(output_path, run_id, {
            "requested_per_status": per_status,
            "token_budget": token_budget,
            "validated_counts": {status: len(samples) for status, samples in selected.items()},
            "validation_records": records,
        })
        persisted_record_count = len(records)

    async def execute(
        sample: CodeNetSample,
        source: str,
        candidate_type: str,
    ) -> tuple[str, str, str | None, dict[str, Any] | None, dict[str, Any] | None]:
        for attempt in range(1, max(1, max_attempts) + 1):
            result = await runner.execute_single_submission(
                f"validate_{candidate_type}",
                client,
                archetype=f"validation_{candidate_type}",
                timeout_s=180.0,
                source_code=source,
                assignment_id=assignments[sample.problem_id],
                llm_backend="local",
                action="validate",
            )
            if result.final_status == "COMPLETED" and result.sandbox_output is not None:
                break
            if attempt < max(1, max_attempts):
                await asyncio.sleep(retry_backoff_seconds * 2 ** (attempt - 1))
        actual = _sandbox_stage(result.sandbox_output)
        evidence = _sandbox_evidence(result.sandbox_output)
        record = {
            "problem_id": sample.problem_id,
            "expected_status": "AC" if candidate_type == "reference" else sample.status,
            "candidate_type": candidate_type,
            "submission_id": sample.accepted_submission_id if candidate_type == "reference" else sample.submission_id,
            "validation_submission_id": result.submission_id,
            "final_status": result.final_status,
            "actual_status": actual,
            "error_message": result.error_message,
            "sandbox_evidence": evidence,
        }
        if candidate_type == "reference" or result.final_status != "COMPLETED" or result.sandbox_output is None:
            records.append(record)
            persist()
        if result.final_status != "COMPLETED" or result.sandbox_output is None:
            LOGGER.warning(
                "Sandbox validation did not complete for %s %s after %d attempts: %s (%s); skipping.",
                sample.problem_id, candidate_type, max_attempts, result.final_status,
                result.error_message or "no sandbox output",
            )
            return "UNKNOWN", result.submission_id, result.error_message, evidence, None
        return actual, result.submission_id, result.error_message, evidence, result.sandbox_output

    async with httpx.AsyncClient(base_url=config.target_url, timeout=30.0) as client:
        for sample in candidate_pool:
            if len(selected.get(sample.status, [])) >= per_status:
                continue
            reference_key = (sample.problem_id, sample.accepted_submission_id)
            if reference_key not in accepted_cache:
                reference_status, validation_id, _, reference_evidence, reference_output = await execute(
                    sample, sample.read_accepted_source(), "reference"
                )
                accepted_cache[reference_key] = (
                    reference_status, validation_id, reference_evidence, reference_output
                )
            (
                reference_status, reference_validation_id, reference_evidence, reference_output
            ) = accepted_cache[reference_key]
            if reference_status != "AC":
                records.append({
                    "problem_id": sample.problem_id,
                    "expected_status": sample.status,
                    "candidate_type": "candidate",
                    "submission_id": sample.submission_id,
                    "actual_status": "REJECTED",
                    "reason": f"accepted reference validated as {reference_status}, not AC",
                    "reference_validation_submission_id": reference_validation_id,
                    "reference_sandbox_evidence": reference_evidence,
                })
                persist()
                continue

            if sample.status == "AC":
                actual_status = reference_status
                validation_id = reference_validation_id
                candidate_evidence = reference_evidence
                candidate_output = reference_output
            else:
                actual_status, validation_id, _, candidate_evidence, candidate_output = await execute(
                    sample, sample.read_source(), "candidate"
                )
            estimated_tokens = estimate_context_prompt_tokens(
                sample.description, sample.read_source(), candidate_output
            )
            status_matches = actual_status == sample.status
            within_budget = estimated_tokens < token_budget
            accepted = status_matches and within_budget
            records.append({
                "problem_id": sample.problem_id,
                "expected_status": sample.status,
                "candidate_type": "candidate",
                "submission_id": sample.submission_id,
                "validation_submission_id": validation_id,
                "reference_validation_submission_id": reference_validation_id,
                "actual_status": actual_status,
                "accepted": accepted,
                "estimated_context_prompt_tokens": estimated_tokens,
                "within_token_budget": within_budget,
                "reason": (
                    None if accepted
                    else f"sandbox stage {actual_status} does not match {sample.status}" if not status_matches
                    else f"estimated prompt {estimated_tokens} tokens >= budget {token_budget}"
                ),
                "sandbox_evidence": candidate_evidence,
            })
            if accepted:
                selected.setdefault(sample.status, []).append(sample)
            persist()
            if len(selected.get(sample.status, [])) >= per_status:
                continue

    counts = {status: len(selected.get(status, [])) for status in sorted({s.status for s in candidate_pool})}
    persist(force=True)
    insufficient = {status: count for status, count in counts.items() if count < per_status}
    if insufficient:
        details = ", ".join(f"{status}={count}/{per_status}" for status, count in insufficient.items())
        LOGGER.warning(
            "Sandbox validation found fewer stage-matching samples than requested: %s. "
            "Continuing with the validated samples; see %s.", details, output_path,
        )
    validated = [
        sample
        for status_samples in selected.values()
        for sample in status_samples[:per_status]
    ]
    return validated, records


async def run(args: argparse.Namespace) -> Path:
    resolve_teacher_token(args.target_url)  # fail fast before the long archive scan
    run_id = f"codenet_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    output_dir = args.results_dir.expanduser().resolve() / run_id
    output_dir.mkdir(parents=True, exist_ok=False)
    candidate_pool = load_archive_samples(
        args.archive,
        args.cache_dir,
        per_status=args.per_status,
        statuses=tuple(args.statuses),
        seed=args.seed,
        candidate_pool_multiplier=args.validation_candidate_multiplier,
    )
    models = _model_list(args.models)
    provisioner = AssignmentProvisioner(args.target_url, resolve_teacher_token(args.target_url))
    assignments, fixture_assignment_id = await provisioner.ensure_assignments(
        candidate_pool,
        include_fixtures=not args.skip_fixtures,
    )
    if not assignments:
        raise RuntimeError("No CodeNet assignments were provisioned.")
    if not args.skip_fixtures and fixture_assignment_id is None:
        raise RuntimeError("The benchmark fixture assignment was not provisioned.")

    config = BenchmarkConfig(
        target_url=args.target_url,
        results_dir=output_dir,
        user_pool_size=max(args.virtual_users, args.burst_size),
        user_prefix=args.user_prefix,
        password=os.getenv("BENCHMARK_USER_PASSWORD", "BenchPass123!"),
        suite_name="codenet",
        hardware_tag=args.hardware_tag,
        notes="Project CodeNet stratified C submissions with SWCP fixture and QoS telemetry.",
    )
    auth_config = BenchmarkConfig(
        target_url=args.target_url,
        results_dir=args.cache_dir.expanduser().resolve() / "auth",
        user_pool_size=config.user_pool_size,
        user_prefix=config.user_prefix,
        password=config.password,
    )
    tokens = await Seeder(auth_config).ensure_users_and_tokens(count=config.user_pool_size)
    progress = ProgressTracker(
        args.progress_file, 0, [model.key for model in models], run_id, every=args.progress_every,
    )
    progress.set_phase("validation", note=f"Validating CodeNet candidates (token budget {args.token_budget})")
    samples, validation_records = await _validate_candidate_pool(
        candidate_pool,
        assignments,
        tokens,
        config,
        output_dir / "validation.json",
        run_id,
        args.per_status,
        args.token_budget,
        args.max_attempts,
        args.retry_backoff_seconds,
    )
    if not samples:
        raise RuntimeError("No CodeNet samples passed sandbox validation and the token preflight.")
    shortfall = {
        status: args.per_status - sum(sample.status == status for sample in samples)
        for status in args.statuses
    }
    if any(shortfall.values()):
        LOGGER.warning("Validated fewer samples than requested: %s", {k: v for k, v in shortfall.items() if v})
    _write_samples_manifest(output_dir / "samples.json", samples)
    burst_submissions_per_model = len(_select_burst_samples(samples, args.burst_size))
    model_manager = LocalModelManager(namespace=args.namespace)
    controls = RunControls(
        max_attempts=args.max_attempts,
        retry_backoff_seconds=args.retry_backoff_seconds,
        warmup_requests=args.warmup_requests,
        progress=progress,
    )
    telemetry = TelemetryCollector(config)
    model_runs: list[dict[str, Any]] = []
    metadata = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_value("rev-parse", "HEAD"),
        "git_branch": _git_value("branch", "--show-current"),
        "hardware_tag": args.hardware_tag,
        "dataset_archive": str(args.archive.resolve()),
        "dataset_seed": args.seed,
        "per_status": args.per_status,
        "validation_candidate_multiplier": args.validation_candidate_multiplier,
        "validated_sample_count": len(samples),
        "validation_record_count": len(validation_records),
        "statuses": list(args.statuses),
        "context_modes": list(args.context_modes),
        "models": [model.key for model in models],
        "assignment_count": len(assignments) + int(fixture_assignment_id is not None),
        "submission_count_expected": (
            len(samples) * len(models) * len(args.context_modes)
            + (len(models) * len(FIXTURES) * len(args.context_modes) if not args.skip_fixtures else 0)
            + len(models) * burst_submissions_per_model
        ),
        "prometheus_scrape_interval_seconds": 15,
        "burst_size": args.burst_size,
        "burst_submissions_per_model": burst_submissions_per_model,
        "token_budget": args.token_budget,
        "max_attempts": args.max_attempts,
        "warmup_requests_per_model": args.warmup_requests,
        "validated_counts": {status: sum(s.status == status for s in samples) for status in args.statuses},
    }
    progress.total = metadata["submission_count_expected"]
    _write_run_summary(output_dir / "run_metadata.json", run_id, metadata)

    try:
        with JsonlWriter(output_dir / "results.jsonl") as writer:
            for model in models:
                LOGGER.info("Starting benchmark for %s (%s).", model.display_name, model.backend)
                progress.set_phase("model_swap", model.key, f"Switching to {model.display_name}")
                try:
                    model_manager.switch_to(model)
                except Exception as exc:  # noqa: BLE001 - skip the model, keep the sweep alive
                    LOGGER.error("Could not switch to %s: %s; skipping it.", model.key, exc)
                    model_runs.append({"model_key": model.key, "skipped": True, "error": str(exc)[:500]})
                    continue
                model_result = await _run_model(
                    model,
                    samples,
                    assignments,
                    fixture_assignment_id,
                    tokens,
                    config,
                    run_id,
                    writer,
                    telemetry,
                    args.context_modes,
                    args.burst_size,
                    args.burst_window_seconds,
                    controls,
                )
                model_runs.append(model_result)
                metadata["model_runs"] = model_runs
                _write_run_summary(output_dir / "run_metadata.json", run_id, metadata)
                (output_dir / f"prometheus_{model.key}.json").write_text(
                    json.dumps(model_result["prometheus"], indent=2) + "\n",
                    encoding="utf-8",
                )
    finally:
        progress.set_phase("restore", note="Restoring the original vLLM deployment")
        model_manager.restore()

    metadata["ended_at_utc"] = datetime.now(timezone.utc).isoformat()
    metadata["model_runs"] = model_runs
    metadata["model_swaps"] = model_manager.swap_log
    _write_run_summary(output_dir / "run_metadata.json", run_id, metadata)
    summary = write_summary(output_dir / "results.jsonl", output_dir)
    LOGGER.info("Wrote %d summary rows to %s.", len(summary), output_dir)

    if not args.skip_judge:
        try:
            judge_config = resolve_judge_config(args)
            await judge_run(
                output_dir, judge_config, args.judge_concurrency, args.judge_line_tolerance, progress,
            )
        except Exception as exc:  # noqa: BLE001 - benchmark data is already persisted
            LOGGER.error("LLM-as-a-judge stage failed: %s", exc)
            progress.set_phase("judging", note=f"Judge failed: {str(exc)[:200]}")
    if not args.skip_report:
        from apps.benchmark.report import generate_report

        progress.set_phase("report", note="Generating figures and tables")
        try:
            report_dir = generate_report(output_dir, report_options_from_args(args))
            LOGGER.info("Report written to %s.", report_dir)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Report generation failed: %s", exc)
    progress.set_phase("done", note=f"Results in {output_dir}")
    return output_dir


def report_options_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "electricity_usd_per_kwh": args.electricity_usd_per_kwh,
        "gpu_capex_usd": args.gpu_capex_usd,
        "gpu_lifetime_hours": args.gpu_lifetime_hours,
        "azure_input_usd_per_mtok": args.azure_input_usd_per_mtok,
        "azure_output_usd_per_mtok": args.azure_output_usd_per_mtok,
    }


def _write_samples_manifest(path: Path, samples: list[CodeNetSample]) -> None:
    """Material the judge needs per CodeNet submission (problem, student code, accepted reference)."""
    manifest = {
        sample.submission_id: {
            "problem_id": sample.problem_id,
            "title": sample.title,
            "status": sample.status,
            "accepted_submission_id": sample.accepted_submission_id,
            "description": sample.description,
            "sample_input": sample.sample_input,
            "expected_stdout": sample.expected_stdout,
            "source": sample.read_source(),
            "accepted_source": sample.read_accepted_source(),
        }
        for sample in samples
    }
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _env_float(name: str) -> float | None:
    value = os.getenv(name)
    return float(value) if value else None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=Path("/home/test/Downloads/Project_CodeNet.tar.gz"))
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "swcp-benchmark" / "codenet")
    parser.add_argument("--results-dir", type=Path, default=Path("benchmarks/results"))
    parser.add_argument("--target-url", default=os.getenv("TARGET_URL", "http://localhost"))
    parser.add_argument("--namespace", default=os.getenv("K8S_NAMESPACE", "swcp"))
    parser.add_argument("--hardware-tag", default=os.getenv("HARDWARE_TAG", "rtx5070ti-16gb"))
    parser.add_argument("--user-prefix", default=os.getenv("BENCHMARK_USER_PREFIX", "bench_user_"))
    parser.add_argument("--virtual-users", type=int, default=4)
    parser.add_argument("--per-status", type=int, default=100)
    parser.add_argument("--validation-candidate-multiplier", type=int, default=10)
    parser.add_argument("--statuses", nargs="+", choices=DEFAULT_STATUSES, default=list(RUBRIC_STATUSES))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--models", nargs="+", choices=sorted(MODELS))
    parser.add_argument(
        "--context-modes",
        nargs="+",
        choices=("with_context_transfer", "raw_code_only"),
        default=["with_context_transfer"],
    )
    parser.add_argument("--burst-size", type=int, default=4)
    parser.add_argument("--burst-window-seconds", type=float, default=5.0)
    parser.add_argument("--skip-fixtures", action="store_true")
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET,
                        help="Reject candidates whose estimated context-transfer prompt is not below this.")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=5.0)
    parser.add_argument("--warmup-requests", type=int, default=2)
    parser.add_argument(
        "--progress-file", type=Path, default=None,
        help=f"Optional live Markdown progress file (disabled by default), e.g. {DEFAULT_PROGRESS_FILE}.",
    )
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--skip-report", action="store_true")
    add_judge_arguments(parser)
    cost = parser.add_argument_group("cost model (report)")
    cost.add_argument("--electricity-usd-per-kwh", type=float, default=0.30)
    cost.add_argument("--gpu-capex-usd", type=float, default=900.0)
    cost.add_argument("--gpu-lifetime-hours", type=float, default=26_280.0)
    cost.add_argument("--azure-input-usd-per-mtok", type=float, default=_env_float("AZURE_INPUT_USD_PER_MTOK"))
    cost.add_argument("--azure-output-usd-per-mtok", type=float, default=_env_float("AZURE_OUTPUT_USD_PER_MTOK"))
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING"))
    args = parser.parse_args()
    if args.max_attempts <= 0 or args.warmup_requests < 0 or args.token_budget <= 0:
        parser.error("--max-attempts and --token-budget must be positive; --warmup-requests must be >= 0.")
    if args.per_status <= 0 or args.virtual_users <= 0 or args.validation_candidate_multiplier <= 0:
        parser.error("--per-status, --virtual-users, and --validation-candidate-multiplier must be greater than zero.")
    if not 0 <= args.burst_size <= 4:
        parser.error("--burst-size must be between 0 and 4 for the configured vLLM sequence limit.")
    if args.burst_window_seconds <= 0:
        parser.error("--burst-window-seconds must be greater than zero.")
    return args


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    output_dir = asyncio.run(run(args))
    print(output_dir)


if __name__ == "__main__":
    main()
