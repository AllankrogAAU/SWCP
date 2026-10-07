"""
What an evaluation produces, independent of how the API chooses to
serialize it. Keeping this separate from the Pydantic response model
in api/submissions.py means evaluation logic doesn't depend on FastAPI
at all -- useful once this gets called from a queue worker instead of
an HTTP handler, which has no Pydantic response model to fill in.
"""

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
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None


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


def from_run(compile_result: RunResult, run_result: RunResult) -> EvaluationResult:
    status = EvaluationStatus.RUN_TIMEOUT if run_result.timed_out else EvaluationStatus.SUCCESS
    return EvaluationResult(
        status=status,
        compile_stdout=compile_result.stdout,
        compile_stderr=compile_result.stderr,
        exit_code=run_result.exit_code,
        stdout=run_result.stdout,
        stderr=run_result.stderr,
    )