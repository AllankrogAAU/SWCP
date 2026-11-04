import logging
import re
from collections.abc import Sequence
from typing import Any

from contracts import SandboxTestCase, StaticAnalysisWarning
from evaluation.result import EvaluationResult, SandboxReport, Status
from sandbox.interface import Limits, SandboxError, SandboxProvider

logger = logging.getLogger("c-sandbox-worker")
_STATIC_ANALYSIS_PATTERN = re.compile(
    r"^(?P<line>\d+):(?P<column>\d+):\s+(?P<severity>[^:]+):\s+"
    r"(?P<message>.*?)(?:\s+\[[^]]+\])?$"
)


def parse_static_analysis(output: str) -> tuple[StaticAnalysisWarning, ...]:
    warnings = []
    for line in output.splitlines():
        match = _STATIC_ANALYSIS_PATTERN.match(line)
        if match is not None:
            warnings.append(
                StaticAnalysisWarning(
                    line=int(match["line"]),
                    column=int(match["column"]),
                    severity=match["severity"].strip(),
                    message=match["message"].strip(),
                )
            )
    return tuple(warnings)


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

    lint_output = None
    static_analysis: tuple[StaticAnalysisWarning, ...] = ()
    if report.lint is not None:
        lint_output = "lint timed out" if report.lint.timed_out else report.lint.stderr
        if not report.lint.timed_out:
            static_analysis = parse_static_analysis(report.lint.stderr)
    if report.test_runs:
        return EvaluationResult(
            status=(
                Status.RUN_TIMEOUT
                if any(test_run.timed_out for test_run in report.test_runs)
                else Status.SUCCESS
            ),
            **base,
            lint_output=lint_output,
            static_analysis=static_analysis,
            run_ms=sum(test_run.duration_ms for test_run in report.test_runs),
            test_runs=report.test_runs,
        )

    run = report.run
    if run is None:
        raise SandboxError("sandbox produced neither a run phase nor test results")
    run_fields: dict[str, Any] = {
        "lint_output": lint_output,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "run_ms": run.duration_ms,
    }
    if run.timed_out:
        return EvaluationResult(
            status=Status.RUN_TIMEOUT,
            static_analysis=static_analysis,
            **base,
            **run_fields,
        )
    return EvaluationResult(
        status=Status.SUCCESS,
        exit_code=run.rc,
        static_analysis=static_analysis,
        **base,
        **run_fields,
    )


def evaluate(
    source: str,
    provider: SandboxProvider,
    limits: Limits | None = None,
    test_cases: Sequence[SandboxTestCase] = (),
    harness_source: str | None = None,
    run_stdin: str = "",
) -> EvaluationResult:
    limits = limits or Limits()
    last_error: SandboxError | None = None
    for attempt in (1, 2):
        sandbox = provider.acquire()
        try:
            report = sandbox.run(source, limits, test_cases, harness_source, run_stdin)
            if (
                test_cases
                and report.compile is not None
                and not report.compile.timed_out
                and report.compile.rc == 0
                and len(report.test_runs) != len(test_cases)
            ):
                raise SandboxError(
                    f"sandbox returned {len(report.test_runs)} test results "
                    f"for {len(test_cases)} test cases"
                )
            return to_result(report)
        except SandboxError as exception:
            last_error = exception
            logger.warning("Sandbox attempt %d failed: %s", attempt, exception)
        finally:
            sandbox.destroy()
    raise SandboxError(f"sandbox failed after retry: {last_error}")
