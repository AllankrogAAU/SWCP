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
    
    @abstractmethod
    def write_file(self, filename: str, content: str) -> None:
        ...

    @abstractmethod
    def run(self, cmd: list[str], timeout: float) -> RunResult:
         ...

    @abstractmethod
    def destroy(self) -> None:
        ...