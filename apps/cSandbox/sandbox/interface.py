from dataclasses import dataclass
from typing import Protocol

from evaluation.result import SandboxReport


class SandboxError(Exception):
    """Execution inside the sandbox failed for infrastructure reasons."""


class SandboxStartError(SandboxError):
    """The sandbox could not be created or never became ready."""


@dataclass(frozen=True)
class Limits:
    compile_timeout_s: int = 10
    lint_timeout_s: int = 10
    run_timeout_s: int = 2
    output_cap_bytes: int = 65536


class Sandbox(Protocol):
    def run(self, source: str, limits: Limits) -> SandboxReport: ...

    def destroy(self) -> None: ...


class SandboxProvider(Protocol):
    def acquire(self) -> Sandbox: ...
