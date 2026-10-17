import logging
from typing import Any

from evaluation.result import EvaluationResult, SandboxReport, Status
from sandbox.interface import Limits, SandboxError, SandboxProvider

logger = logging.getLogger("c-sandbox-worker")


def to_result(report: SandboxReport) -> EvaluationResult:
    compile_ = report.compile
    if compile_ is None:
        raise SandboxError("sandbox report contained no compile phase")

    base: dict[str, Any] = {
        "compile_stdout": compile_.stdout,
        "compile_stderr": compile_.stderr,
        "compile_ms": compile_.duration_ms,
    }
    if compile_.timed_out:
        return EvaluationResult(status=Status.COMPILE_TIMEOUT, **base)
    if compile_.rc != 0:
        return EvaluationResult(status=Status.COMPILE_ERROR, **base)

    run = report.run
    if run is None:
        raise SandboxError("sandbox report contained no run phase")
    lint_output = None
    if report.lint is not None:
        lint_output = "lint timed out" if report.lint.timed_out else report.lint.stderr
    run_fields: dict[str, Any] = {
        "lint_output": lint_output,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "run_ms": run.duration_ms,
    }
    if run.timed_out:
        return EvaluationResult(status=Status.RUN_TIMEOUT, **base, **run_fields)
    return EvaluationResult(
        status=Status.SUCCESS, exit_code=run.rc, **base, **run_fields
    )


def evaluate(
    source: str, provider: SandboxProvider, limits: Limits | None = None
) -> EvaluationResult:
    limits = limits or Limits()
    last_error: SandboxError | None = None
    for attempt in (1, 2):
        sandbox = provider.acquire()
        try:
            return to_result(sandbox.run(source, limits))
        except SandboxError as exception:
            last_error = exception
            logger.warning("Sandbox attempt %d failed: %s", attempt, exception)
        finally:
            sandbox.destroy()
    raise SandboxError(f"sandbox failed after retry: {last_error}")
