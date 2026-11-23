import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from kubernetes.client.rest import ApiException
import pytest

from contracts import SandboxTask, SandboxTestCase
from evaluation.evaluator import evaluate
from evaluation.result import EvaluationResult, PhaseResult, SandboxReport, Status
from sandbox.interface import (
    Limits,
    PermanentSandboxError,
    SandboxError,
    SandboxStartError,
)
from sandbox.k8s_pod import (
    KubePodSandbox,
    KubeSandboxProvider,
    PodConfig,
    build_pod_manifest,
    classify_api_error,
)
from sandbox.report import parse_report
from sandbox.runsc_oci import RunscConfig, RunscSandbox, build_oci_spec
from worker import evaluate_task, handle_message


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def phase(rc: int = 0, out: str = "", err: str = "", ms: int = 5) -> PhaseResult:
    return PhaseResult(rc=rc, duration_ms=ms, stdout=out, stderr=err)


class FakeSandbox:
    def __init__(self, report: SandboxReport | Exception) -> None:
        self.report = report
        self.destroyed = False

    def run(
        self,
        source: str,
        limits: Limits,
        test_cases=(),
        harness_source=None,
        run_stdin="",
    ) -> SandboxReport:
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


def test_parse_report_decodes_test_runs() -> None:
    raw = "\n".join([
        "compile_rc=0",
        "compile_ms=12",
        f"compile_stdout={b64('')}",
        f"compile_stderr={b64('')}",
        "test_0_rc=0",
        "test_0_ms=7",
        f"test_0_stdout={b64('5' + chr(10))}",
        f"test_0_stderr={b64('')}",
        "test_1_rc=124",
        "test_1_ms=2000",
        f"test_1_stdout={b64('')}",
        f"test_1_stderr={b64('')}",
    ])
    report = parse_report(raw)
    assert report.test_runs == (phase(out="5\n", ms=7), phase(rc=124, ms=2000))


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


def test_test_case_timeout_is_reported() -> None:
    cases = [SandboxTestCase(stdin="", expectedStdout="ok\n", timeoutMilliseconds=2000, memoryLimitMegabytes=64)]
    report = SandboxReport(phase(), phase(), None, (phase(rc=124, ms=2000),))
    result = evaluate("x", FakeProvider(report), test_cases=cases)
    assert result.status is Status.RUN_TIMEOUT
    assert result.test_runs == report.test_runs


def test_test_case_count_mismatch_is_permanent() -> None:
    cases = [SandboxTestCase(stdin="", expectedStdout="ok\n", timeoutMilliseconds=2000, memoryLimitMegabytes=64)]
    provider = FakeProvider(
        SandboxReport(phase(), phase(), None),
    )
    with pytest.raises(PermanentSandboxError, match="returned 0 test results"):
        evaluate("x", provider, test_cases=cases)


def test_worker_compares_each_test_case_output(monkeypatch) -> None:
    task = SandboxTask(
        submissionId="00000000-0000-0000-0000-000000000001",
        assignmentId="00000000-0000-0000-0000-000000000002",
        source="int main(void) { return 0; }",
        compilerFlags=[],
        testCases=[
            SandboxTestCase(
                stdin="2 3\n",
                expectedStdout="5\n",
                timeoutMilliseconds=2000,
                memoryLimitMegabytes=64,
            ),
            SandboxTestCase(
                stdin="-4 9\n",
                expectedStdout="5\n",
                timeoutMilliseconds=2000,
                memoryLimitMegabytes=64,
            ),
        ],
    )
    test_runs = (phase(out="5\n"), phase(out="13\n"))
    monkeypatch.setattr(
        "worker.evaluate",
        lambda *_args: EvaluationResult(status=Status.SUCCESS, test_runs=test_runs),
    )

    result = evaluate_task(task, object())

    assert [test.passed for test in result.testResults] == [True, False]
    assert [test.stdout for test in result.testResults] == ["5\n", "13\n"]


def test_worker_passes_assignment_harness_and_run_stdin(monkeypatch) -> None:
    task = SandboxTask(
        submissionId="00000000-0000-0000-0000-000000000001",
        assignmentId="00000000-0000-0000-0000-000000000002",
        source="int add(int a, int b) { return a + b; }",
        compilerFlags=[],
        testCases=[],
        harnessSource="int main(void) { return 0; }",
        runStdin="2 3\n",
    )
    received = {}

    def fake_evaluate(source, provider, limits, test_cases, harness_source, run_stdin):
        received.update(
            source=source,
            test_cases=test_cases,
            harness_source=harness_source,
            run_stdin=run_stdin,
        )
        return EvaluationResult(
            status=Status.SUCCESS,
            exit_code=0,
            stdout="5\n",
            stderr="",
            run_ms=1,
        )

    monkeypatch.setattr("worker.evaluate", fake_evaluate)

    result = evaluate_task(task, object())

    assert received == {
        "source": task.source,
        "test_cases": [],
        "harness_source": task.harnessSource,
        "run_stdin": "2 3\n",
    }
    assert result.testResults[0].stdout == "5\n"


def test_evaluator_leaves_transient_retry_to_worker() -> None:
    provider = FakeProvider(
        SandboxError("exec died"), SandboxReport(phase(), phase(), phase())
    )
    first, second = provider.sandboxes

    with pytest.raises(SandboxError, match="exec died"):
        evaluate("x", provider)

    assert first.destroyed
    assert provider.sandboxes == [second]


def test_permanent_failure_is_not_retried() -> None:
    provider = FakeProvider(PermanentSandboxError("invalid provider state"))

    with pytest.raises(PermanentSandboxError, match="invalid provider state"):
        evaluate("x", provider)

    assert provider.sandboxes == []


def test_kubernetes_api_status_is_classified() -> None:
    permanent = classify_api_error(
        "create pod", ApiException(status=403, reason="Forbidden")
    )
    transient = classify_api_error(
        "create pod", ApiException(status=503, reason="Unavailable")
    )

    assert isinstance(permanent, PermanentSandboxError)
    assert type(transient) is SandboxError


def test_permanent_pod_read_failure_survives_provider_acquire(monkeypatch) -> None:
    class FakeApi:
        def create_namespaced_pod(self, *_args, **_kwargs) -> None:
            pass

        def delete_namespaced_pod(self, *_args, **_kwargs) -> None:
            pass

    provider = make_provider(FakeApi())
    monkeypatch.setattr(provider, "_should_spill_from_headroom_node", lambda: False)

    def fail_waiting(_name: str) -> None:
        raise PermanentSandboxError("forbidden")

    monkeypatch.setattr(provider, "_wait_running", fail_waiting)
    with pytest.raises(PermanentSandboxError, match="forbidden"):
        provider._create()


def test_worker_dead_letters_permanent_failure_without_retry(monkeypatch) -> None:
    task = SandboxTask(
        submissionId="00000000-0000-0000-0000-000000000001",
        assignmentId="00000000-0000-0000-0000-000000000002",
        source="int main(void) { return 0; }",
        compilerFlags=[],
        testCases=[],
    )

    class FakeMessage:
        data = task.model_dump_json().encode("utf-8")
        headers = {}
        metadata = SimpleNamespace(num_delivered=1)

        def __init__(self) -> None:
            self.acked = False
            self.nacked = False

        async def ack(self) -> None:
            self.acked = True

        async def nak(self) -> None:
            self.nacked = True

    class FakeConnection:
        async def publish(self, *_args, **_kwargs) -> None:
            pass

    class FakeJetStream:
        def __init__(self) -> None:
            self.published = []

        async def publish(self, subject, data, **kwargs) -> None:
            self.published.append((subject, data, kwargs))

    def fail_evaluation(*_args):
        raise PermanentSandboxError("permanent provider configuration")

    monkeypatch.setattr("worker.evaluate_task", fail_evaluation)
    message = FakeMessage()
    jetstream = FakeJetStream()

    async def run() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            await handle_message(message, FakeConnection(), jetstream, object(), executor)

    asyncio.run(run())

    assert message.acked
    assert not message.nacked
    assert [subject for subject, _, _ in jetstream.published] == [
        "sandbox.tasks.dlq",
        "sandbox.results",
    ]


def test_result_publish_retries_without_rerunning_sandbox(monkeypatch) -> None:
    task = SandboxTask(
        submissionId="00000000-0000-0000-0000-000000000001",
        assignmentId="00000000-0000-0000-0000-000000000002",
        source="int main(void) { return 0; }",
        compilerFlags=[],
        testCases=[],
    )

    class FakeResult:
        errorClassification = "success"

        def model_dump_json(self) -> str:
            return "{}"

    class FakeMessage:
        data = task.model_dump_json().encode("utf-8")
        headers = {}
        metadata = SimpleNamespace(num_delivered=1)

        def __init__(self) -> None:
            self.acked = False
            self.in_progress_count = 0

        async def ack(self) -> None:
            self.acked = True

        async def in_progress(self) -> None:
            self.in_progress_count += 1

    class FakeConnection:
        async def publish(self, *_args, **_kwargs) -> None:
            pass

    class FakeJetStream:
        def __init__(self) -> None:
            self.result_publish_count = 0

        async def publish(self, subject, *_args, **_kwargs) -> None:
            if subject == "sandbox.results":
                self.result_publish_count += 1
                if self.result_publish_count == 1:
                    raise RuntimeError("temporary NATS error")

    evaluate_mock = Mock(return_value=FakeResult())
    monkeypatch.setattr("worker.evaluate_task", evaluate_mock)

    async def no_sleep(_delay: float) -> None:
        pass

    monkeypatch.setattr("worker.asyncio.sleep", no_sleep)
    message = FakeMessage()
    jetstream = FakeJetStream()

    async def run() -> None:
        with ThreadPoolExecutor(max_workers=1) as executor:
            await handle_message(message, FakeConnection(), jetstream, object(), executor)

    asyncio.run(run())

    evaluate_mock.assert_called_once()
    assert jetstream.result_publish_count == 2
    assert message.in_progress_count == 1
    assert message.acked


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
    preferred = spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert preferred[0]["weight"] == 100
    assert preferred[0]["preference"]["matchExpressions"][0] == {
        "key": "node-role.kubernetes.io/control-plane",
        "operator": "Exists",
    }


def test_pod_manifest_without_image_pull_secret() -> None:
    spec = build_pod_manifest(PodConfig(image_pull_secret=""), "sbx-1")["spec"]
    assert "imagePullSecrets" not in spec


def test_pod_manifest_without_runtime_class_for_local_dev() -> None:
    spec = build_pod_manifest(PodConfig(runtime_class=""), "sbx-1")["spec"]
    assert "runtimeClassName" not in spec


def test_pod_sandbox_closes_stdin_after_sending_input(monkeypatch) -> None:
    class FakeApi:
        def connect_get_namespaced_pod_exec(self) -> None:
            pass

    class FakeWebSocket:
        def __init__(self) -> None:
            self.input = b""
            self.closed_channel = None
            self.stdout = "compile_rc=0\ncompile_ms=1\nrun_rc=0\nrun_ms=1\n"
            self.closed = False

        def write_stdin(self, data: bytes) -> None:
            self.input = data

        def close_channel(self, channel: int) -> None:
            self.closed_channel = channel

        def is_open(self) -> bool:
            return False

        def peek_stdout(self) -> bool:
            return bool(self.stdout)

        def read_stdout(self) -> str:
            output, self.stdout = self.stdout, ""
            return output

        def close(self) -> None:
            self.closed = True

    websocket = FakeWebSocket()
    monkeypatch.setattr("sandbox.k8s_pod.stream", lambda *_args, **_kwargs: websocket)
    monkeypatch.setattr("sandbox.k8s_pod.client.ApiClient", lambda: object())
    monkeypatch.setattr("sandbox.k8s_pod.client.CoreV1Api", lambda _api: FakeApi())

    sandbox = KubePodSandbox(PodConfig(), "sbx-1", object())
    sandbox.run("int main(void) { return 0; }", Limits(), run_stdin="input")

    assert websocket.input.endswith(b"input")
    assert websocket.closed_channel == 0
    assert websocket.closed


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
    with pytest.raises(PermanentSandboxError) as excinfo:
        provider._wait_running("sbx-failed")
    message = str(excinfo.value)
    assert "pod entered phase Failed" in message
    assert "ImagePullBackOff" in message


def test_pod_manifest_prefers_owning_worker_node_when_set() -> None:
    spec = build_pod_manifest(PodConfig(node_name="worker-node"), "sbx-1")["spec"]
    preferred = spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert preferred[0]["weight"] == 100
    assert preferred[0]["preference"]["matchExpressions"] == [
        {
            "key": "kubernetes.io/hostname",
            "operator": "In",
            "values": ["worker-node"],
        }
    ]


def test_parallel_warm_pool_fill_respects_pool_size_and_cleans_up(monkeypatch) -> None:
    import threading
    import time

    created = []
    destroyed = []
    max_in_flight = 0
    in_flight = 0
    lock = threading.Lock()

    class FakeWarmSandbox:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.created_at = time.monotonic()

        def destroy(self) -> None:
            with lock:
                destroyed.append(self.idx)

    def fake_create(self):
        nonlocal in_flight, max_in_flight
        with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
            idx = len(created)
            sbx = FakeWarmSandbox(idx)
            created.append(sbx)
        return sbx

    monkeypatch.setattr("sandbox.k8s_pod.load_kube_config", lambda: None)
    monkeypatch.setattr("sandbox.k8s_pod._core_api", lambda: object())
    monkeypatch.setattr(KubeSandboxProvider, "cleanup_stale", lambda self, max_age_s=1800: None)
    monkeypatch.setattr(KubeSandboxProvider, "_create", fake_create)

    provider = KubeSandboxProvider(PodConfig(), pool_size=4, pool_workers=3)
    try:
        deadline = time.monotonic() + 2.0
        while provider._ready.qsize() < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        assert provider._ready.qsize() == 4
        assert len(created) == 4
        assert max_in_flight >= 2
    finally:
        provider.close()
    assert len(destroyed) == 4


def test_pod_manifest_overflow_prefers_non_control_plane_nodes() -> None:
    spec = build_pod_manifest(
        PodConfig(node_name="control-node"), "sbx-1", overflow_to_worker=True
    )["spec"]
    preferred = spec["affinity"]["nodeAffinity"]["preferredDuringSchedulingIgnoredDuringExecution"]
    assert preferred[0]["weight"] == 100
    assert preferred[0]["preference"]["matchExpressions"] == [
        {
            "key": "node-role.kubernetes.io/control-plane",
            "operator": "DoesNotExist",
        }
    ]


def test_headroom_soft_cap_triggers_spillover_only_above_threshold() -> None:
    import threading

    class FakeSpec:
        def __init__(self, node_name: str | None) -> None:
            self.node_name = node_name

    class FakeNamespacePod:
        def __init__(self, node_name: str | None, phase: str) -> None:
            self.spec = FakeSpec(node_name)
            self.status = FakePodStatus(phase)

    class FakePodList:
        def __init__(self, items) -> None:
            self.items = items

    class HeadroomApi:
        def __init__(self, items) -> None:
            self._items = items

        def list_namespaced_pod(self, namespace: str) -> FakePodList:
            return FakePodList(self._items)

    def make_headroom_provider(node_name: str, items: list[FakeNamespacePod]):
        provider = object.__new__(KubeSandboxProvider)
        provider._cfg = PodConfig(
            node_name=node_name,
            headroom_node="control-node",
            node_soft_max_pods=2,
        )
        provider._api = HeadroomApi(items)
        provider._headroom_lock = threading.Lock()
        provider._headroom_checked_at = 0.0
        provider._headroom_spill = False
        return provider

    under = make_headroom_provider(
        "control-node",
        [
            FakeNamespacePod("control-node", "Running"),
            FakeNamespacePod("control-node", "Succeeded"),
            FakeNamespacePod("worker-node", "Running"),
        ],
    )
    assert under._should_spill_from_headroom_node() is False

    at_cap = make_headroom_provider(
        "control-node",
        [
            FakeNamespacePod("control-node", "Running"),
            FakeNamespacePod("control-node", "Pending"),
        ],
    )
    assert at_cap._should_spill_from_headroom_node() is True

    non_headroom = make_headroom_provider(
        "worker-node",
        [
            FakeNamespacePod("control-node", "Running"),
            FakeNamespacePod("control-node", "Running"),
        ],
    )
    assert non_headroom._should_spill_from_headroom_node() is False


def test_runsc_oci_spec_is_hardened_and_readonly() -> None:
    cfg = RunscConfig(rootfs_path="/opt/sandbox-rootfs", memory_limit_mb=512)
    spec = build_oci_spec(cfg, ["/usr/local/bin/run-submission", "10"], "/tmp/sbx-1/work")
    assert spec["root"] == {"path": "/opt/sandbox-rootfs", "readonly": True}
    assert spec["process"]["user"] == {"uid": 10001, "gid": 10001}
    assert spec["process"]["noNewPrivileges"] is True
    assert spec["process"]["capabilities"]["effective"] == []
    tmp_mount = next(m for m in spec["mounts"] if m["destination"] == "/tmp")
    assert tmp_mount["source"] == "/tmp/sbx-1/work"
    assert "rw" in tmp_mount["options"] and "nosuid" in tmp_mount["options"]


def test_runsc_sandbox_pipes_stdin_and_cleans_up(monkeypatch, tmp_path) -> None:
    import subprocess

    captured = {}

    def fake_run(cmd, **kwargs):
        if "run" in cmd:
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input", b"")
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b"compile_rc=0\ncompile_ms=5\nrun_rc=0\nrun_ms=2\n", stderr=b""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("sandbox.runsc_oci.subprocess.run", fake_run)
    cfg = RunscConfig(
        work_base_dir=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        rootless=False,
    )
    sbx = RunscSandbox(cfg)
    try:
        report = sbx.run("int main(void) { return 0; }", Limits(), run_stdin="hello")
        assert report.compile is not None and report.compile.rc == 0
        assert captured["input"].endswith(b"hello")
        assert "--network=none" in captured["cmd"]
        assert "--overlay2=none" in captured["cmd"]
        assert sbx._bundle_dir is not None and sbx._bundle_dir.exists()
    finally:
        sbx.destroy()
    assert sbx._bundle_dir is not None and not sbx._bundle_dir.exists()

