"""Deterministic comparison of CodeNet labels with SWCP sandbox observations."""

from __future__ import annotations

from typing import Any


def observed_status(
    sandbox_output: dict[str, Any] | None,
    timeout_ms: int,
    memory_limit_mb: int,
) -> str:
    if not sandbox_output:
        return "UNKNOWN"

    classification = str(sandbox_output.get("errorClassification") or "").lower()
    if classification.startswith("worker-error:") or classification == "compile_timeout":
        return "UNKNOWN"
    if "compile" in classification:
        return "CE"
    if "timeout" in classification or "time limit" in classification:
        return "TLE"
    if "memory" in classification or "oom" in classification:
        return "MLE"
    if "runtime" in classification or "signal" in classification:
        return "RE"

    compilation = sandbox_output.get("compilation") or {}
    if not compilation.get("success", False):
        return "CE"

    results = sandbox_output.get("testResults")
    if not isinstance(results, list) or not results:
        return "AC"

    for result in results:
        if not isinstance(result, dict):
            continue
        duration = result.get("durationMilliseconds")
        memory = result.get("memoryConsumedMegabytes")
        exit_code = result.get("exitCode")
        if isinstance(duration, (int, float)) and duration >= timeout_ms:
            return "TLE"
        if isinstance(memory, (int, float)) and memory >= memory_limit_mb:
            return "MLE"
        if exit_code not in (0, None):
            return "RE"
        if not result.get("passed", False):
            return "WA"
    return "AC"


def judge_sample(
    expected_status: str,
    sandbox_output: dict[str, Any] | None,
    timeout_ms: int,
    memory_limit_mb: int,
    task_solved: bool | None,
    context_mode: str,
) -> dict[str, Any]:
    observed = observed_status(sandbox_output, timeout_ms, memory_limit_mb)
    expected_verdict = True if observed == "AC" and context_mode == "with_context_transfer" else None
    verdict_policy_match = task_solved is expected_verdict
    return {
        "expected_status": expected_status,
        "observed_status": observed,
        "status_match": observed == expected_status,
        "task_solved_expected": expected_verdict,
        "task_solved_policy_match": verdict_policy_match,
        "sandbox_available": sandbox_output is not None,
    }
