"""
The sandbox contract. evaluator.py, compiler.py, and executor.py should
only ever talk to this interface — never to Docker (or, later,
Kubernetes) directly. That's what lets a future KubernetesSandbox
replace DockerSandbox without touching evaluation logic at all.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RunResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float


class Sandbox(ABC):
    """One sandbox instance = one isolated environment for exactly one
    submission. Compile and execute both happen inside the same
    instance via separate run() calls — the sandbox is created once,
    used for both phases, then always destroyed."""

    @abstractmethod
    def write_file(self, filename: str, content: str) -> None:
        """Write a text file into the sandbox's writable working directory."""
        ...

    @abstractmethod
    def run(self, cmd: list[str], timeout: float) -> RunResult:
        """Run a command inside the sandbox, relative to its working
        directory. Call this multiple times against the same instance
        (e.g. once to compile, once to execute) to share one container
        across both phases of a submission."""
        ...

    @abstractmethod
    def destroy(self) -> None:
        """Tear down the sandbox. Must be safe to call even if the
        sandbox is mid-timeout or in an otherwise bad state — this is
        what guarantees no process outlives the submission, regardless
        of what happened in run()."""
        ...