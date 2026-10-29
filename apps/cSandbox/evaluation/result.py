from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    SUCCESS = "success"
    COMPILE_ERROR = "compile_error"
    COMPILE_TIMEOUT = "compile_timeout"
    RUN_TIMEOUT = "run_timeout"


@dataclass(frozen=True)
class PhaseResult:
    rc: int
    duration_ms: int
    stdout: str
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.rc == 124


@dataclass(frozen=True)
class SandboxReport:
    compile: PhaseResult | None
    lint: PhaseResult | None
    run: PhaseResult | None
    test_runs: tuple[PhaseResult, ...] = ()


@dataclass(frozen=True)
class EvaluationResult:
    status: Status
    compile_stdout: str | None = None
    compile_stderr: str | None = None
    compile_ms: int | None = None
    lint_output: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    run_ms: int | None = None
    test_runs: tuple[PhaseResult, ...] = ()
