import logging
import os
import socket
import struct
import uuid

import docker
from docker.errors import DockerException, NotFound
from docker.models.containers import Container

from evaluation.result import SandboxReport
from sandbox.interface import Limits, SandboxError, SandboxStartError
from sandbox.report import parse_report

logger = logging.getLogger("c-sandbox-worker")


class DockerSandbox:
    def __init__(self, container: Container) -> None:
        self._container = container

    def run(self, source: str, limits: Limits) -> SandboxReport:
        source_data = source.encode("utf-8")
        try:
            command = [
                "run-submission",
                str(len(source_data)),
                str(limits.compile_timeout_s),
                str(limits.lint_timeout_s),
                str(limits.run_timeout_s),
                str(limits.output_cap_bytes),
            ]
            exec_stream = self._container.exec_run(
                command,
                stdin=True,
                socket=True,
                user="10001:10001",
            ).output
            try:
                exec_stream._sock.sendall(source_data)
                exec_stream._sock.shutdown(socket.SHUT_WR)
                stdout, stderr = _read_exec_output(exec_stream)
            finally:
                exec_stream.close()
        except SandboxError:
            raise
        except (DockerException, OSError) as exception:
            raise SandboxError(f"Docker sandbox execution failed: {exception}") from exception

        if stderr:
            logger.debug("Docker sandbox runner stderr: %s", stderr.decode("utf-8", errors="replace"))
        report = parse_report(stdout.decode("utf-8", errors="replace"))
        if report.compile is None:
            raise SandboxError("sandbox produced no report")
        return report

    def destroy(self) -> None:
        try:
            self._container.remove(force=True)
        except NotFound:
            pass
        except DockerException:
            logger.exception("Failed to remove Docker sandbox %s", self._container.name)


class DockerSandboxProvider:
    def __init__(self, image: str, client: docker.DockerClient | None = None) -> None:
        self._image = image
        self._client = client or docker.from_env(
            timeout=int(os.getenv("SANDBOX_DOCKER_TIMEOUT_S", "60"))
        )

    @classmethod
    def from_env(cls) -> "DockerSandboxProvider":
        return cls(os.getenv("SANDBOX_IMAGE", "c-sandbox-runtime:latest"))

    def acquire(self) -> DockerSandbox:
        container: Container | None = None
        try:
            container = self._client.containers.create(
                image=self._image,
                name=f"swcp-sandbox-{uuid.uuid4().hex[:12]}",
                command=["sleep", "infinity"],
                detach=True,
                stdin_open=False,
                tty=False,
                user="10001:10001",
                working_dir="/tmp",
                read_only=True,
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit=os.getenv("SANDBOX_MEMORY_LIMIT", "256m"),
                memswap_limit=os.getenv("SANDBOX_MEMORY_LIMIT", "256m"),
                nano_cpus=int(os.getenv("SANDBOX_CPU_LIMIT_NANOS", "1000000000")),
                pids_limit=int(os.getenv("SANDBOX_PIDS_LIMIT", "64")),
                tmpfs={
                    "/tmp": (
                        "rw,exec,nosuid,mode=1777,size="
                        f"{os.getenv('SANDBOX_TMP_SIZE_LIMIT', '64m')}"
                    )
                },
                labels={"app.kubernetes.io/name": "swcp-sandbox"},
                restart_policy={"Name": "no"},
            )
            container.start()
            return DockerSandbox(container)
        except DockerException as exception:
            if container is not None:
                try:
                    container.remove(force=True)
                except NotFound:
                    pass
                except DockerException:
                    logger.exception("Failed to clean up an unstarted Docker sandbox")
            raise SandboxStartError(f"could not start Docker sandbox: {exception}") from exception

    def close(self) -> None:
        self._client.close()


def _read_exec_output(stream) -> tuple[bytes, bytes]:
    output = stream.read()
    stdout = bytearray()
    stderr = bytearray()
    offset = 0
    while offset < len(output):
        if len(output) - offset < 8:
            raise SandboxError("Docker returned a truncated exec stream header")
        stream_type = output[offset]
        frame_size = struct.unpack(">I", output[offset + 4 : offset + 8])[0]
        frame_start = offset + 8
        frame_end = frame_start + frame_size
        if frame_end > len(output):
            raise SandboxError("Docker returned a truncated exec stream frame")
        if stream_type == 1:
            stdout.extend(output[frame_start:frame_end])
        elif stream_type in (2, 3):
            stderr.extend(output[frame_start:frame_end])
        else:
            raise SandboxError(f"Docker returned an unknown exec stream type {stream_type}")
        offset = frame_end
    return bytes(stdout), bytes(stderr)
