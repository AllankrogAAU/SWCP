import logging
import os
import queue
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

from contracts import SandboxTestCase
from evaluation.result import SandboxReport
from sandbox.interface import Limits, SandboxError, SandboxStartError
from sandbox.report import parse_report

logger = logging.getLogger("c-sandbox-worker")

JOB_LABEL = {"app.kubernetes.io/name": "c-sandbox-job"}
CREATED_AT_ANNOTATION = "swcp.io/created-at"


@dataclass(frozen=True)
class PodConfig:
    namespace: str = "swcp-sandbox"
    image: str = "c-sandbox-runtime:latest"
    runtime_class: str = "gvisor"
    cpu_limit: str = "1"
    memory_limit: str = "256Mi"
    tmp_size_limit: str = "64Mi"
    ready_timeout_s: float = 30.0
    owner: str = "worker"
    image_pull_secret: str = "ghcr-pull-secret"

    @staticmethod
    def from_env() -> "PodConfig":
        return PodConfig(
            namespace=os.getenv("SANDBOX_NAMESPACE", "swcp-sandbox"),
            image=os.getenv("SANDBOX_IMAGE", "c-sandbox-runtime:latest"),
            runtime_class=os.getenv("SANDBOX_RUNTIME_CLASS", "gvisor"),
            cpu_limit=os.getenv("SANDBOX_CPU_LIMIT", "1"),
            memory_limit=os.getenv("SANDBOX_MEMORY_LIMIT", "256Mi"),
            tmp_size_limit=os.getenv("SANDBOX_TMP_SIZE_LIMIT", "64Mi"),
            ready_timeout_s=float(os.getenv("SANDBOX_READY_TIMEOUT_S", "30")),
            owner=os.getenv("POD_NAME", "worker"),
            image_pull_secret=os.getenv(
                "SANDBOX_IMAGE_PULL_SECRET", "ghcr-pull-secret"
            ),
        )


def build_pod_manifest(cfg: PodConfig, name: str) -> dict:
    spec: dict = {
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 0,
        "activeDeadlineSeconds": 900,
        "hostNetwork": False,
        "hostPID": False,
        "hostIPC": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "sandbox",
                "image": cfg.image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sleep", "infinity"],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "resources": {
                    "requests": {"cpu": "50m", "memory": "128Mi"},
                    "limits": {"cpu": cfg.cpu_limit, "memory": cfg.memory_limit},
                },
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            }
        ],
        "volumes": [
            {
                "name": "tmp",
                "emptyDir": {"medium": "Memory", "sizeLimit": cfg.tmp_size_limit},
            }
        ],
    }
    if cfg.runtime_class:
        spec["runtimeClassName"] = cfg.runtime_class
    if cfg.image_pull_secret:
        spec["imagePullSecrets"] = [{"name": cfg.image_pull_secret}]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {**JOB_LABEL, "swcp.io/owner": cfg.owner[:63]},
            "annotations": {CREATED_AT_ANNOTATION: str(int(time.time()))},
        },
        "spec": spec,
    }


def _core_api() -> client.CoreV1Api:
    return client.CoreV1Api(client.ApiClient())


def load_kube_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


class KubePodSandbox:
    def __init__(self, cfg: PodConfig, name: str, api: client.CoreV1Api) -> None:
        self._cfg = cfg
        self.name = name
        self._api = api
        self.created_at = time.monotonic()

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
            "run-submission",
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
        exec_api = client.CoreV1Api(client.ApiClient())
        try:
            ws = stream(
                exec_api.connect_get_namespaced_pod_exec,
                self.name,
                self._cfg.namespace,
                command=command,
                container="sandbox",
                stderr=True,
                stdin=True,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            try:
                ws.write_stdin(bytes(input_data))
                ws.close_channel(0)
                deadline = time.monotonic() + deadline_s
                chunks: list[str] = []
                while ws.is_open():
                    if time.monotonic() > deadline:
                        raise SandboxError("sandbox exec exceeded its deadline")
                    ws.update(timeout=1)
                    if ws.peek_stdout():
                        chunks.append(ws.read_stdout())
                if ws.peek_stdout():
                    chunks.append(ws.read_stdout())
            finally:
                ws.close()
        except SandboxError:
            raise
        except Exception as exception:
            raise SandboxError(f"exec failed: {exception}") from exception

        report = parse_report("".join(chunks))
        if report.compile is None:
            raise SandboxError("sandbox produced no report")
        return report

    def destroy(self) -> None:
        try:
            self._api.delete_namespaced_pod(
                self.name,
                self._cfg.namespace,
                grace_period_seconds=0,
                body=client.V1DeleteOptions(grace_period_seconds=0),
            )
        except ApiException as exception:
            if exception.status != 404:
                logger.warning(
                    "Failed to delete sandbox pod %s: %s", self.name, exception
                )


class KubeSandboxProvider:
    def __init__(
        self, cfg: PodConfig, pool_size: int = 0, max_idle_s: float = 600.0
    ) -> None:
        load_kube_config()
        self._cfg = cfg
        self._api = _core_api()
        self._pool_size = pool_size
        self._max_idle_s = max_idle_s
        self._ready: queue.Queue[KubePodSandbox] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.cleanup_stale()
        if pool_size > 0:
            self._start_thread(self._fill_loop, "sandbox-pool-fill")
        self._start_thread(self._janitor_loop, "sandbox-janitor")

    def _start_thread(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _create(self) -> KubePodSandbox:
        name = f"sbx-{uuid.uuid4().hex[:12]}"
        sandbox = KubePodSandbox(self._cfg, name, self._api)
        try:
            self._api.create_namespaced_pod(
                self._cfg.namespace,
                build_pod_manifest(self._cfg, name),  # type: ignore[arg-type]
            )
            self._wait_running(name)
        except Exception as exception:
            sandbox.destroy()
            raise SandboxStartError(
                f"could not start sandbox pod: {exception}"
            ) from exception
        return sandbox

    def _diagnose(self, name: str) -> str:
        parts: list[str] = []
        try:
            pod = self._api.read_namespaced_pod_status(name, self._cfg.namespace)
            if pod.status:
                for condition in pod.status.conditions or []:
                    if condition.status != "True":
                        parts.append(
                            f"condition {condition.type}={condition.status} "
                            f"({condition.reason}: {condition.message})"
                        )
                for container_status in pod.status.container_statuses or []:
                    waiting = (
                        container_status.state.waiting
                        if container_status.state
                        else None
                    )
                    if waiting:
                        parts.append(
                            f"container waiting: {waiting.reason} ({waiting.message})"
                        )
        except ApiException as exception:
            parts.append(f"could not read pod status: {exception}")

        try:
            events = self._api.list_namespaced_event(
                self._cfg.namespace,
                field_selector=f"involvedObject.name={name}",
            )
            for event in events.items[-5:]:
                parts.append(f"event: {event.reason} - {event.message}")
        except ApiException as exception:
            parts.append(f"could not list pod events: {exception}")

        return "; ".join(parts) if parts else "no diagnostic information available"

    def _wait_running(self, name: str) -> None:
        deadline = time.monotonic() + self._cfg.ready_timeout_s
        while time.monotonic() < deadline:
            pod = self._api.read_namespaced_pod_status(name, self._cfg.namespace)
            phase = pod.status.phase if pod.status else None
            if phase == "Running":
                return
            if phase in ("Failed", "Succeeded"):
                raise SandboxStartError(
                    f"pod entered phase {phase}: {self._diagnose(name)}"
                )
            time.sleep(0.1)
        raise SandboxStartError(
            f"pod did not become Running within {self._cfg.ready_timeout_s}s: {self._diagnose(name)}"
        )

    def acquire(self):
        while True:
            try:
                sandbox = self._ready.get_nowait()
            except queue.Empty:
                return self._create()
            if time.monotonic() - sandbox.created_at < self._max_idle_s:
                return sandbox
            sandbox.destroy()

    def _fill_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self._ready.qsize() < self._pool_size:
                    self._ready.put(self._create())
                    continue
            except Exception:
                logger.exception("Warm pool fill failed")
                self._stop.wait(5)
                continue
            self._stop.wait(0.5)

    def _janitor_loop(self) -> None:
        while not self._stop.wait(300):
            self.cleanup_stale()

    def cleanup_stale(self, max_age_s: int = 1800) -> None:
        try:
            pods = self._api.list_namespaced_pod(
                self._cfg.namespace,
                label_selector=f"app.kubernetes.io/name={JOB_LABEL['app.kubernetes.io/name']}",
            )
        except Exception:
            logger.exception("Sandbox janitor could not list pods")
            return
        now = time.time()
        for pod in pods.items:
            meta = pod.metadata
            if meta is None or meta.name is None:
                continue
            created = float((meta.annotations or {}).get(CREATED_AT_ANNOTATION, 0))
            if now - created > max_age_s:
                KubePodSandbox(self._cfg, meta.name, self._api).destroy()

    def close(self) -> None:
        self._stop.set()
        while True:
            try:
                self._ready.get_nowait().destroy()
            except queue.Empty:
                return
