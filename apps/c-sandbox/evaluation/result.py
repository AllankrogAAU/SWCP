from dataclasses import dataclass
from enum import Enum

from sandbox.interface import RunResult


class EvaluationStatus(str, Enum):
    SUCCESS = "success"            # compiled and ran; see exit_code for the program's own result
    COMPILE_ERROR = "compile_error"
    COMPILE_TIMEOUT = "compile_timeout"
    RUN_TIMEOUT = "run_timeout"


@dataclass
class EvaluationResult:
    status: EvaluationStatus
    compile_stdout: str | None = None
    compile_stderr: str | None = None
    lint_output: str | None = None      # new
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None


def _lint_text(lint_result: RunResult) -> str:
    if lint_result.timed_out:
        return "lint timed out"
    return lint_result.stderr


def from_compile_failure(compile_result: RunResult) -> EvaluationResult:
    status = (
        EvaluationStatus.COMPILE_TIMEOUT
        if compile_result.timed_out
        else EvaluationStatus.COMPILE_ERROR
    )
    return EvaluationResult(
        status=status,
        compile_stdout=compile_result.stdout,
        compile_stderr=compile_result.stderr,
    )


def from_run(compile_result: RunResult, run_result: RunResult, lint_result: RunResult) -> EvaluationResult:
    status = EvaluationStatus.RUN_TIMEOUT if run_result.timed_out else EvaluationStatus.SUCCESS
    return EvaluationResult(
        status=status,
        compile_stdout=compile_result.stdout,
        compile_stderr=compile_result.stderr,
        lint_output=_lint_text(lint_result),
        exit_code=run_result.exit_code,
        stdout=run_result.stdout,
        stderr=run_result.stderr,
    )