import base64

import pytest

from evaluation.evaluator import evaluate
from evaluation.result import PhaseResult, SandboxReport, Status
from sandbox.interface import Limits, SandboxError, SandboxStartError
from sandbox.k8s_pod import KubeSandboxProvider, PodConfig, build_pod_manifest
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
    result = evaluate("x", FakeProvider(SandboxReport(phase(), phase(err="note"), phase(0, "ok"))))
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
    assert spec["imagePullSecrets"] == [{"name": "ghcr-pull-secret"}]


def test_pod_manifest_without_image_pull_secret() -> None:
    spec = build_pod_manifest(PodConfig(image_pull_secret=""), "sbx-1")["spec"]
    assert "imagePullSecrets" not in spec


def test_pod_manifest_without_runtime_class_for_local_dev() -> None:
    spec = build_pod_manifest(PodConfig(runtime_class=""), "sbx-1")["spec"]
    assert "runtimeClassName" not in spec


def test_lint_timeout_is_reported_as_text() -> None:
    report = SandboxReport(phase(), phase(124), phase())
    assert evaluate("x", FakeProvider(report)).lint_output == "lint timed out"


def test_clean_lint_is_empty_string_not_none() -> None:
    assert evaluate("x", FakeProvider(SandboxReport(phase(), phase(), phase()))).lint_output == ""


class FakeWaiting:
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message


class FakeContainerState:
    def __init__(self, waiting: FakeWaiting | None) -> None:
        self.waiting = waiting


class FakeContainerStatus:
    def __init__(self, waiting: FakeWaiting | None) -> None:
        self.state = FakeContainerState(waiting)


class FakeCondition:
    def __init__(self, type_: str, status: str, reason: str, message: str) -> None:
        self.type = type_
        self.status = status
        self.reason = reason
        self.message = message


class FakePodStatus:
    def __init__(self, phase, conditions=None, container_statuses=None) -> None:
        self.phase = phase
        self.conditions = conditions or []
        self.container_statuses = container_statuses or []


class FakePod:
    def __init__(self, status: FakePodStatus) -> None:
        self.status = status


class FakeEvent:
    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        self.message = message


class FakeEventList:
    def __init__(self, items: list[FakeEvent]) -> None:
        self.items = items


class FakeKubeApi:
    def __init__(self, pod: FakePod, events: list[FakeEvent] | None = None) -> None:
        self._pod = pod
        self._events = events or []

    def read_namespaced_pod_status(self, name: str, namespace: str) -> FakePod:
        return self._pod

    def list_namespaced_event(self, namespace: str, field_selector: str | None = None) -> FakeEventList:
        return FakeEventList(self._events)


def make_provider(api: FakeKubeApi) -> KubeSandboxProvider:
    provider = object.__new__(KubeSandboxProvider)
    provider._cfg = PodConfig(ready_timeout_s=0.2)
    provider._api = api
    return provider


def test_wait_running_succeeds_once_pod_is_running() -> None:
    provider = make_provider(FakeKubeApi(FakePod(FakePodStatus("Running"))))
    provider._wait_running("sbx-ok")  # should not raise


def test_wait_running_timeout_surfaces_scheduling_diagnostics() -> None:
    conditions = [
        FakeCondition("PodScheduled", "False", "Unschedulable", "0/1 nodes match node selector"),
    ]
    api = FakeKubeApi(
        FakePod(FakePodStatus("Pending", conditions=conditions)),
        events=[FakeEvent("FailedScheduling", "no nodes have the swcp.io/gvisor label")],
    )
    provider = make_provider(api)
    with pytest.raises(SandboxStartError) as excinfo:
        provider._wait_running("sbx-pending")
    message = str(excinfo.value)
    assert "Unschedulable" in message
    assert "FailedScheduling" in message
    assert "swcp.io/gvisor" in message


def test_wait_running_failed_phase_surfaces_container_wait_reason() -> None:
    container_statuses = [FakeContainerStatus(FakeWaiting("ImagePullBackOff", "manifest unknown"))]
    api = FakeKubeApi(FakePod(FakePodStatus("Failed", container_statuses=container_statuses)))
    provider = make_provider(api)
    with pytest.raises(SandboxStartError) as excinfo:
        provider._wait_running("sbx-failed")
    message = str(excinfo.value)
    assert "pod entered phase Failed" in message
    assert "ImagePullBackOff" in message

