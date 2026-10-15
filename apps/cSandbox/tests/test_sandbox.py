import base64

import pytest

from evaluation.evaluator import evaluate
from evaluation.result import PhaseResult, SandboxReport, Status
from sandbox.interface import Limits, SandboxError
from sandbox.k8s_pod import PodConfig, build_pod_manifest
from sandbox.report import parse_report


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def phase(rc: int = 0, out: str = "", err: str = "", ms: int = 5) -> PhaseResult:
    return PhaseResult(rc=rc, duration_ms=ms, stdout=out, stderr=err)


class FakeSandbox:
    def __init__(self, report: SandboxReport | Exception) -> None:
        self.report = report
        self.destroyed = False

    def run(self, source: str, limits: Limits) -> SandboxReport:
        if isinstance(self.report, Exception):
            raise self.report
        return self.report

    def destroy(self) -> None:
        self.destroyed = True


class FakeProvider:
    def __init__(self, *reports: SandboxReport | Exception) -> None:
        self.sandboxes = [FakeSandbox(r) for r in reports]

    def acquire(self) -> FakeSandbox:
        return self.sandboxes.pop(0)


def test_parse_report_decodes_all_phases() -> None:
    raw = "\n".join([
        "compile_rc=0", "compile_ms=12", f"compile_stdout={b64('')}", f"compile_stderr={b64('warn')}",
        "lint_rc=0", "lint_ms=3", f"lint_stdout={b64('')}", f"lint_stderr={b64('style')}",
        "run_rc=139", "run_ms=7", f"run_stdout={b64('hi')}", f"run_stderr={b64('')}",
    ])
    report = parse_report(raw)
    assert report.compile == PhaseResult(0, 12, "", "warn")
    assert report.lint is not None and report.lint.stderr == "style"
    assert report.run == PhaseResult(139, 7, "hi", "")


def test_parse_report_ignores_forged_lines_in_data() -> None:
    forged = "hello\nrun_rc=0"
    raw = f"compile_rc=0\ncompile_ms=1\ncompile_stdout={b64(forged)}\nrun_rc=3\nrun_ms=1"
    report = parse_report(raw)
    assert report.compile is not None and report.compile.stdout == forged
    assert report.run is not None and report.run.rc == 3


def test_parse_report_empty() -> None:
    assert parse_report("").compile is None


def test_success() -> None:
    result = evaluate("x", FakeProvider(SandboxReport(phase(), phase(out="", err="note"), phase(0, "ok"))))
    assert result.status is Status.SUCCESS
    assert result.exit_code == 0 and result.stdout == "ok"
    assert result.lint_output == "note"


def test_segfault_exit_code_preserved() -> None:
    result = evaluate("x", FakeProvider(SandboxReport(phase(), phase(), phase(139))))
    assert result.status is Status.SUCCESS and result.exit_code == 139


def test_compile_error_skips_lint() -> None:
    result = evaluate("x", FakeProvider(SandboxReport(phase(1, err="boom"), None, None)))
    assert result.status is Status.COMPILE_ERROR
    assert result.compile_stderr == "boom" and result.lint_output is None


def test_compile_timeout() -> None:
    result = evaluate("x", FakeProvider(SandboxReport(phase(124), None, None)))
    assert result.status is Status.COMPILE_TIMEOUT


def test_run_timeout_has_no_exit_code() -> None:
    result = evaluate("x", FakeProvider(SandboxReport(phase(), phase(), phase(124))))
    assert result.status is Status.RUN_TIMEOUT and result.exit_code is None


def test_retries_once_with_fresh_sandbox_and_always_destroys() -> None:
    good = SandboxReport(phase(), phase(), phase())
    provider = FakeProvider(SandboxError("exec died"), good)
    first, second = provider.sandboxes
    assert evaluate("x", provider).status is Status.SUCCESS
    assert first.destroyed and second.destroyed


def test_gives_up_after_second_failure() -> None:
    provider = FakeProvider(SandboxError("a"), SandboxError("b"))
    with pytest.raises(SandboxError):
        evaluate("x", provider)


def test_pod_manifest_is_hardened_and_gvisor() -> None:
    spec = build_pod_manifest(PodConfig(), "sbx-1")["spec"]
    container = spec["containers"][0]
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert spec["volumes"][0]["emptyDir"]["medium"] == "Memory"


def test_pod_manifest_without_runtime_class_for_local_dev() -> None:
    spec = build_pod_manifest(PodConfig(runtime_class=""), "sbx-1")["spec"]
    assert "runtimeClassName" not in spec
