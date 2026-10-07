import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .interface import RunResult, Sandbox

IMAGE = "c-sandbox-runtime:latest"
SECCOMP_PROFILE = str(Path(__file__).parent.parent / "seccomp-profile.json")

MEMORY_LIMIT = "128m"   # shared budget for both compile and execute phases
PIDS_LIMIT = "64"
CPU_LIMIT = "1"
MAX_OUTPUT_BYTES = 1_000_000

WORKDIR = "/tmp"  # the one writable path (tmpfs) inside the read-only container


class DockerSandbox(Sandbox):
    def __init__(self):
        self.container_name = f"csbx-{uuid.uuid4()}"
        self._start()

    def _start(self) -> None:
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "--network", "none",
            "--read-only",
            #Allow writing for the compilation to work.
            "--tmpfs", f"{WORKDIR}:rw,exec,size=16m,mode=1777",
            "--memory", MEMORY_LIMIT,
            "--memory-swap", MEMORY_LIMIT,
            "--pids-limit", PIDS_LIMIT,
            "--cpus", CPU_LIMIT,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--security-opt", f"seccomp={SECCOMP_PROFILE}",
            IMAGE,
            "sleep", "infinity",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"failed to start sandbox container: {result.stderr}")

    def write_file(self, filename: str, content: str) -> None:
        result = subprocess.run(
        ["docker", "exec", "-i", self.container_name, "sh", "-c", f"cat > {WORKDIR}/{filename}"],
        input=content.encode("utf-8"),
        capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"failed to write {filename} into sandbox: {result.stderr.decode('utf-8', errors='replace')}"
            )

    def run(self, cmd: list[str], timeout: float) -> RunResult:
        full_cmd = ["docker", "exec", "-w", WORKDIR, self.container_name] + cmd

        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(full_cmd, capture_output=True, timeout=timeout)
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            # This kills the local `docker exec` client process, not necessarily the process it started inside the container
            timed_out = True
            stdout, stderr, exit_code = e.stdout or b"", e.stderr or b"", None

        duration = time.monotonic() - start

        return RunResult(
            exit_code=exit_code,
            stdout=(stdout or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            stderr=(stderr or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_seconds=round(duration, 3),
        )

    def destroy(self) -> None:
        # Force-remove regardless of container state (running, stuck, already exited)
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True)