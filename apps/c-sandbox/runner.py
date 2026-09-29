#!/usr/bin/env python3
"""
Executes one pre-compiled student binary inside an isolated, ephemeral
Docker container and returns stdout/stderr/exit code/timing as JSON.

This is the scaffold only: receive binary -> run in sandbox -> return
output. Queueing, multi-node scheduling, and result persistence are
layered on top of this in the distributed system, not inside it.

Usage:
    python3 runner.py /path/to/compiled_binary [--stdin input.txt]
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

IMAGE = "c-sandbox-runtime:latest"
SECCOMP_PROFILE = os.path.join(os.path.dirname(__file__), "seccomp-profile.json")

# Hard resource ceilings — tune per assignment, but never trust a default.
MEMORY_LIMIT = "64m"
PIDS_LIMIT = "32"
CPU_LIMIT = "1"
WALL_TIMEOUT_SECONDS = 5
MAX_OUTPUT_BYTES = 1_000_000  # truncate runaway stdout/stderr

def is_elf_binary(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == b"\x7fELF"

def run_binary(binary_path: str, stdin_path: str | None = None) -> dict:
    if not os.path.isfile(binary_path):
        raise FileNotFoundError(binary_path)

    if not is_elf_binary(binary_path):
        return {
            "run_id": str(uuid.uuid4()),
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": 0.0,
            "stdout": "",
            "stderr": "Input is not a valid ELF executable.",
            "stdout_truncated": False,
            "stderr_truncated": False,
        }

    run_id = str(uuid.uuid4())
    container_name = f"csbx-{run_id}"

    # Stage the binary in its own tempdir so the container only ever
    # sees exactly one file, mounted read-only.
    with tempfile.TemporaryDirectory(prefix="csbx-") as staging:
        staged_bin = os.path.join(staging, "prog")
        shutil.copy2(binary_path, staged_bin)
        os.chmod(staged_bin, 0o555)  # read + execute only, no write

        cmd = [
            "docker", "run",
            "--rm",
            "--name", container_name,
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=8m,mode=1777",
            "--memory", MEMORY_LIMIT,
            "--memory-swap", MEMORY_LIMIT,  # no swap headroom
            "--pids-limit", PIDS_LIMIT,
            "--cpus", CPU_LIMIT,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--security-opt", f"seccomp={SECCOMP_PROFILE}",
            "--ulimit", "cpu=5",
            "--ulimit", "nofile=64:64",
            "-v", f"{staged_bin}:/sandbox/bin/prog:ro",
            IMAGE,
        ]

        stdin_data = None
        if stdin_path:
            with open(stdin_path, "rb") as f:
                stdin_data = f.read()

        start = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                timeout=WALL_TIMEOUT_SECONDS,
            )
            stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            timed_out = True
            stdout, stderr, returncode = e.stdout or b"", e.stderr or b"", None
            # Wall-clock timeout hit — force-kill in case docker's own
            # timeout handling didn't already tear the container down.
            subprocess.run(["docker", "kill", container_name],
                            capture_output=True)
        duration = time.monotonic() - start

    return {
        "run_id": run_id,
        "exit_code": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 3),
        "stdout": stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stdout_truncated": len(stdout) > MAX_OUTPUT_BYTES,
        "stderr_truncated": len(stderr) > MAX_OUTPUT_BYTES,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("binary")
    parser.add_argument("--stdin", default=None)
    args = parser.parse_args()

    result = run_binary(args.binary, args.stdin)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
