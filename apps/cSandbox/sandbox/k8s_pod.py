import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

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

    def run(self, source: str, limits: Limits) -> SandboxReport:
        data = source.encode("utf-8")
        command = [
            "run-submission",
            str(len(data)),
            str(limits.compile_timeout_s),
            str(limits.lint_timeout_s),
            str(limits.run_timeout_s),
            str(limits.output_cap_bytes),
        ]
        deadline_s = (
            limits.compile_timeout_s + limits.lint_timeout_s + limits.run_timeout_s + 15
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
                ws.write_stdin(data)
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

    def _wait_running(self, name: str) -> None:
        deadline = time.monotonic() + self._cfg.ready_timeout_s
        while time.monotonic() < deadline:
            pod = self._api.read_namespaced_pod_status(name, self._cfg.namespace)
            phase = pod.status.phase if pod.status else None
            if phase == "Running":
                return
            if phase in ("Failed", "Succeeded"):
                raise SandboxStartError(f"pod entered phase {phase}")
            time.sleep(0.1)
        raise SandboxStartError("pod did not become Running in time")

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
