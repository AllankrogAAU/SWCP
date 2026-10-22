import os
import socket
import struct
from types import SimpleNamespace

import pytest
from docker.errors import DockerException

from evaluation.evaluator import evaluate
from evaluation.result import PhaseResult, Status
from sandbox.docker import DockerSandbox, DockerSandboxProvider
from sandbox.interface import Limits, SandboxError, SandboxStartError


REPORT = b"\n".join([
    b"compile_rc=0",
    b"compile_ms=3",
    b"compile_stdout=",
    b"compile_stderr=",
    b"lint_rc=0",
    b"lint_ms=2",
    b"lint_stdout=",
    b"lint_stderr=",
    b"run_rc=0",
    b"run_ms=1",
    b"run_stdout=",
    b"run_stderr=",
])


class FakeContainer:
    name = "swcp-sandbox-test"

    def __init__(self, start_error: Exception | None = None) -> None:
        self.start_error = start_error
        self.exec_command: list[str] | None = None
        self.exec_stream = FakeExecStream()
        self.removed = False

    def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error

    def exec_run(self, command: list[str], **kwargs) -> SimpleNamespace:
        self.exec_command = command
        return SimpleNamespace(output=self.exec_stream)

    def remove(self, force: bool = False) -> None:
        self.removed = True


class FakeRawSocket:
    def __init__(self) -> None:
        self.sent: bytes | None = None
        self.shutdown_direction: int | None = None

    def sendall(self, data: bytes) -> None:
        self.sent = data

    def shutdown(self, direction: int) -> None:
        self.shutdown_direction = direction


class FakeExecStream:
    def __init__(self, output: bytes = REPORT) -> None:
        self._sock = FakeRawSocket()
        self.output = output
        self.closed = False

    def read(self) -> bytes:
        return _frame(1, self.output)

    def close(self) -> None:
        self.closed = True


class FakeContainers:
    def __init__(self, container: FakeContainer) -> None:
        self.container = container
        self.options: dict[str, object] = {}

    def create(self, **options) -> FakeContainer:
        self.options = options
        return self.container


class FakeDockerClient:
    def __init__(self, container: FakeContainer) -> None:
        self.containers = FakeContainers(container)
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_docker_multiplexed_stream_frame_format() -> None:
    assert _frame(2, b"warning") == struct.pack(">B3xI", 2, 7) + b"warning"


def test_docker_sandbox_runs_and_parses_report() -> None:
    container = FakeContainer()
    sandbox = DockerSandbox(container)  # type: ignore[arg-type]

    source = "int main(void) {}"
    report = sandbox.run(source, Limits())

    assert container.exec_command is not None
    assert container.exec_stream._sock.sent == source.encode()
    assert container.exec_stream._sock.shutdown_direction == socket.SHUT_WR
    assert container.exec_stream.closed
    assert report.compile == PhaseResult(0, 3, "", "")
    assert report.run == PhaseResult(0, 1, "", "")


def test_docker_provider_applies_container_isolation_and_closes_client() -> None:
    container = FakeContainer()
    client = FakeDockerClient(container)
    provider = DockerSandboxProvider("c-sandbox-runtime:latest", client)  # type: ignore[arg-type]

    sandbox = provider.acquire()
    options = client.containers.options

    assert options["network_mode"] == "none"
    assert options["read_only"] is True
    assert options["user"] == "10001:10001"
    assert options["cap_drop"] == ["ALL"]
    assert options["security_opt"] == ["no-new-privileges:true"]
    assert options["pids_limit"] == 64
    sandbox.destroy()
    provider.close()
    assert container.removed and client.closed


def test_docker_provider_cleans_up_when_container_start_fails() -> None:
    container = FakeContainer(DockerException("start failed"))
    provider = DockerSandboxProvider(
        "c-sandbox-runtime:latest", FakeDockerClient(container)  # type: ignore[arg-type]
    )

    with pytest.raises(SandboxStartError, match="could not start Docker sandbox"):
        provider.acquire()

    assert container.removed


def test_docker_sandbox_rejects_missing_report() -> None:
    container = FakeContainer()
    container.exec_stream.output = b""
    sandbox = DockerSandbox(container)  # type: ignore[arg-type]

    with pytest.raises(SandboxError, match="sandbox produced no report"):
        sandbox.run("int main(void) {}", Limits())


@pytest.mark.skipif(
    os.getenv("SANDBOX_DOCKER_INTEGRATION") != "1",
    reason="set SANDBOX_DOCKER_INTEGRATION=1 to run the Docker integration test",
)
def test_docker_provider_executes_source_in_the_runtime_image() -> None:
    provider = DockerSandboxProvider.from_env()
    try:
        result = evaluate(
            '#include <stdio.h>\nint main(void) { puts("sandbox-ok"); return 0; }\n',
            provider,
        )
    finally:
        provider.close()

    assert result.status is Status.SUCCESS
    assert result.exit_code == 0
    assert result.stdout == "sandbox-ok\n"


def _frame(channel: int, data: bytes) -> bytes:
    return struct.pack(">B3xI", channel, len(data)) + data
