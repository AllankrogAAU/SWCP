import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from contracts import SandboxTestCase
from evaluation.result import SandboxReport
from sandbox.interface import Limits, PermanentSandboxError, SandboxError
from sandbox.report import parse_report

logger = logging.getLogger("c-sandbox-worker")


@dataclass(frozen=True)
class RunscConfig:
    runsc_bin: str = "runsc"
    rootfs_path: str = "/opt/sandbox-rootfs"
    work_base_dir: str = "/tmp"
    state_dir: str = "/tmp/runsc-state"
    platform: str = "systrap"
    memory_limit_mb: int = 512
    fsize_limit_bytes: int = 64 * 1024 * 1024
    nproc_limit: int = 128
    nofile_limit: int = 256
    rootless: bool = False

    @staticmethod
    def from_env() -> "RunscConfig":
        default_rootless = os.geteuid() != 0 if hasattr(os, "geteuid") else False
        rootless_env = os.getenv("SANDBOX_RUNSC_ROOTLESS", "").strip().lower()
        if rootless_env in ("1", "true", "yes"):
            rootless = True
        elif rootless_env in ("0", "false", "no"):
            rootless = False
        else:
            rootless = default_rootless

        return RunscConfig(
            runsc_bin=os.getenv("SANDBOX_RUNSC_BIN", "runsc"),
            rootfs_path=os.getenv("SANDBOX_ROOTFS_PATH", "/opt/sandbox-rootfs"),
            work_base_dir=os.getenv("SANDBOX_WORK_BASE_DIR", "/tmp"),
            state_dir=os.getenv("SANDBOX_RUNSC_STATE_DIR", "/tmp/runsc-state"),
            platform=os.getenv("SANDBOX_RUNSC_PLATFORM", "systrap"),
            memory_limit_mb=int(os.getenv("SANDBOX_MEMORY_LIMIT_MB", "512")),
            fsize_limit_bytes=int(
                os.getenv("SANDBOX_FSIZE_LIMIT_BYTES", str(64 * 1024 * 1024))
            ),
            nproc_limit=int(os.getenv("SANDBOX_NPROC_LIMIT", "128")),
            nofile_limit=int(os.getenv("SANDBOX_NOFILE_LIMIT", "256")),
            rootless=rootless,
        )


def build_oci_spec(
    cfg: RunscConfig,
    command: list[str],
    work_dir: str,
) -> dict:
    as_limit_bytes = cfg.memory_limit_mb * 1024 * 1024
    return {
        "ociVersion": "1.0.2",
        "root": {
            "path": cfg.rootfs_path,
            "readonly": True,
        },
        "hostname": "sandbox",
        "process": {
            "terminal": False,
            "user": {"uid": 10001, "gid": 10001},
            "args": command,
            "env": [
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "HOME=/tmp",
                "LANG=C",
            ],
            "cwd": "/tmp",
            "capabilities": {
                "bounding": [],
                "effective": [],
                "inheritable": [],
                "permitted": [],
                "ambient": [],
            },
            "rlimits": [
                {
                    "type": "RLIMIT_AS",
                    "hard": as_limit_bytes,
                    "soft": as_limit_bytes,
                },
                {
                    "type": "RLIMIT_FSIZE",
                    "hard": cfg.fsize_limit_bytes,
                    "soft": cfg.fsize_limit_bytes,
                },
                {
                    "type": "RLIMIT_NPROC",
                    "hard": cfg.nproc_limit,
                    "soft": cfg.nproc_limit,
                },
                {
                    "type": "RLIMIT_NOFILE",
                    "hard": cfg.nofile_limit,
                    "soft": cfg.nofile_limit,
                },
                {
                    "type": "RLIMIT_CORE",
                    "hard": 0,
                    "soft": 0,
                },
            ],
            "noNewPrivileges": True,
        },
        "mounts": [
            {
                "destination": "/proc",
                "type": "proc",
                "source": "proc",
            },
            {
                "destination": "/tmp",
                "type": "bind",
                "source": work_dir,
                "options": ["rbind", "rw", "nosuid", "nodev"],
            },
        ],
        "linux": {
            "namespaces": [
                {"type": "pid"},
                {"type": "ipc"},
                {"type": "uts"},
                {"type": "mount"},
            ],
        },
    }


class RunscSandbox:
    def __init__(self, cfg: RunscConfig) -> None:
        self._cfg = cfg
        self.name = f"sbx-{uuid.uuid4().hex[:12]}"
        self._bundle_dir: Path | None = None

    def _build_runsc_cmd(self, bundle_dir: Path) -> list[str]:
        cmd = [
            self._cfg.runsc_bin,
            f"--root={self._cfg.state_dir}",
            "--network=none",
            "--ignore-cgroups",
            f"--platform={self._cfg.platform}",
            "--file-access=shared",
            "--overlay2=none",
        ]
        if self._cfg.rootless:
            cmd.append("--rootless")
        cmd.extend(["run", "--bundle", str(bundle_dir), self.name])
        return cmd

    def run(
        self,
        source: str,
        limits: Limits,
        test_cases: Sequence[SandboxTestCase] = (),
        harness_source: str | None = None,
        run_stdin: str = "",
    ) -> SandboxReport:
        source_data = source.encode("utf-8")
        harness_data = (harness_source or "").encode("utf-8")
        run_input_data = run_stdin.encode("utf-8")
        input_data = bytearray(source_data)
        input_data.extend(harness_data)
        input_data.extend(run_input_data)

        command = [
            "/usr/local/bin/run-submission",
            str(len(source_data)),
            str(limits.compile_timeout_s),
            str(limits.lint_timeout_s),
            str(limits.run_timeout_s),
            str(limits.output_cap_bytes),
            str(len(test_cases)),
            str(len(harness_data)),
            str(len(run_input_data)),
        ]
        for test_case in test_cases:
            case_input = test_case.stdin.encode("utf-8")
            input_data.extend(case_input)
            command.extend(
                [
                    str(len(case_input)),
                    str(test_case.timeoutMilliseconds),
                    str(test_case.memoryLimitMegabytes),
                ]
            )

        deadline_s = (
            limits.compile_timeout_s
            + limits.lint_timeout_s
            + (limits.run_timeout_s if not test_cases else 0)
            + sum(test_case.timeoutMilliseconds / 1000 + 1 for test_case in test_cases)
            + 15
        )

        os.makedirs(self._cfg.state_dir, exist_ok=True)
        os.makedirs(self._cfg.work_base_dir, exist_ok=True)
        self._bundle_dir = Path(
            tempfile.mkdtemp(prefix=f"{self.name}-", dir=self._cfg.work_base_dir)
        )
        work_dir = self._bundle_dir / "work"
        work_dir.mkdir(mode=0o777, parents=True, exist_ok=True)
        os.chmod(work_dir, 0o777)

        spec = build_oci_spec(self._cfg, command, str(work_dir))
        config_path = self._bundle_dir / "config.json"
        config_path.write_text(json.dumps(spec), encoding="utf-8")

        runsc_cmd = self._build_runsc_cmd(self._bundle_dir)
        try:
            proc = subprocess.run(
                runsc_cmd,
                input=bytes(input_data),
                capture_output=True,
                timeout=deadline_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exception:
            raise SandboxError("sandbox exec exceeded its deadline") from exception
        except Exception as exception:
            raise SandboxError(f"runsc execution failed: {exception}") from exception

        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        report = parse_report(stdout_text)
        if report.compile is None:
            stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
            raise PermanentSandboxError(
                f"sandbox produced no report (rc={proc.returncode}, stderr={stderr_text[:500]})"
            )
        return report

    def destroy(self) -> None:
        try:
            subprocess.run(
                [
                    self._cfg.runsc_bin,
                    f"--root={self._cfg.state_dir}",
                    "delete",
                    "-force",
                    self.name,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception:
            pass
        if self._bundle_dir and self._bundle_dir.exists():
            shutil.rmtree(self._bundle_dir, ignore_errors=True)


class RunscSandboxProvider:
    def __init__(self, cfg: RunscConfig | None = None) -> None:
        self._cfg = cfg or RunscConfig.from_env()
        os.makedirs(self._cfg.state_dir, exist_ok=True)

    def acquire(self) -> RunscSandbox:
        return RunscSandbox(self._cfg)

    def close(self) -> None:
        pass
