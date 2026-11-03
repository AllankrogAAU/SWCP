"""Serial, rollback-safe model selection for the single-GPU vLLM deployment."""

from __future__ import annotations

import json
from dataclasses import dataclass
import logging
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

LOGGER = logging.getLogger("apps.benchmark.model_manager")


@dataclass(frozen=True)
class ModelSpec:
    key: str
    backend: str
    display_name: str
    vllm_name: str | None = None
    vllm_path: str | None = None
    azure_model_key: str | None = None


MODELS = {
    "qwen2.5-coder-3b": ModelSpec(
        "qwen2.5-coder-3b",
        "local",
        "Qwen2.5-Coder-3B-Instruct-AWQ",
        "qwen2.5-coder-3b-instruct-awq",
        "Qwen2.5-Coder-3B-Instruct-AWQ",
    ),
    "qwen2.5-coder-7b": ModelSpec(
        "qwen2.5-coder-7b",
        "local",
        "Qwen2.5-Coder-7B-Instruct-AWQ",
        "qwen2.5-coder-7b-instruct-awq",
        "Qwen2.5-Coder-7B-Instruct-AWQ",
    ),
    "qwen2.5-coder-14b": ModelSpec(
        "qwen2.5-coder-14b",
        "local",
        "Qwen2.5-Coder-14B-Instruct-AWQ",
        "qwen2.5-coder-14b-instruct-awq",
        "Qwen2.5-Coder-14B-Instruct-AWQ",
    ),
    "meta-llama-3.1-8b": ModelSpec(
        "meta-llama-3.1-8b",
        "local",
        "Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
        "meta-llama-3.1-8b-instruct-awq-int4",
        "Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
    ),
    "deepseek-r1-distill-qwen-7b": ModelSpec(
        "deepseek-r1-distill-qwen-7b",
        "local",
        "DeepSeek-R1-Distill-Qwen-7B-AWQ",
        "deepseek-r1-distill-qwen-7b-awq",
        "deepseek-r1-distill-qwen-7b-awq",
    ),
    "azure-gpt-4o": ModelSpec(
        "azure-gpt-4o",
        "azure",
        "Azure GPT-4o",
        azure_model_key="Gpt4o",
    ),
    "azure-gpt-6-sol": ModelSpec(
        "azure-gpt-6-sol",
        "azure",
        "Azure GPT-6-sol",
        azure_model_key="Gpt6Sol",
    ),
}

DEFAULT_CLOUD_MODEL = "azure-gpt-6-sol"


def installed_local_models(model_root: Path | None = None) -> list[ModelSpec]:
    """Local model specs whose AWQ weights are present under ``apps/localLLM/models``."""
    root = model_root or Path(__file__).resolve().parents[1] / "localLLM" / "models"
    return [
        spec for spec in MODELS.values()
        if spec.backend == "local" and spec.vllm_path and (root / spec.vllm_path).is_dir()
    ]


class LocalModelManager:
    def __init__(
        self,
        namespace: str = "swcp",
        model_root: Path | None = None,
        rollout_timeout: str = "15m",
        vram_release_threshold_mb: int = 1_536,
        vram_release_timeout_seconds: float = 240.0,
        health_timeout_seconds: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.namespace = namespace
        self.model_root = model_root or Path(__file__).resolve().parents[1] / "localLLM" / "models"
        self.rollout_timeout = rollout_timeout
        self.vram_release_threshold_mb = vram_release_threshold_mb
        self.vram_release_timeout_seconds = vram_release_timeout_seconds
        self.health_timeout_seconds = health_timeout_seconds
        self._sleep = sleep
        self._original_replicas: int | None = None
        self.swap_log: list[dict[str, Any]] = []
        self._original_env: dict[str, str] | None = None
        self._original_worker_env: dict[str, str] | None = None
        self._modified = False
        self._worker_modified = False

    @staticmethod
    def _run(*args: str) -> str:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
        if result.returncode:
            raise RuntimeError(
                f"Command {' '.join(args[:3])} failed ({result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout.strip()

    def _get_deployment_env(
        self,
        deployment_name: str,
        container_name: str,
        env_names: set[str],
    ) -> dict[str, str]:
        data = json.loads(self._run(
            "kubectl", "get", "deployment", deployment_name, "-n", self.namespace, "-o", "json"
        ))
        containers = data.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
        container = next((item for item in containers if item.get("name") == container_name), None)
        if container is None:
            raise RuntimeError(
                f"The {deployment_name} deployment has no container named '{container_name}'."
            )
        return {
            entry["name"]: entry["value"]
            for entry in container.get("env", [])
            if entry.get("name") in env_names and "value" in entry
        }

    def _configured_model(self) -> tuple[str | None, str | None]:
        data = json.loads(self._run(
            "kubectl", "get", "configmap", "vllm-config", "-n", self.namespace, "-o", "json"
        ))
        values = data.get("data", {})
        return values.get("LLM_MODEL_NAME"), values.get("LLM_MODEL_PATH")

    def _configured_worker_model(self) -> str | None:
        data = json.loads(self._run(
            "kubectl", "get", "configmap", "llm-worker-config", "-n", self.namespace, "-o", "json"
        ))
        values = data.get("data", {})
        return values.get("LocalLlm__ModelName") or values.get("LLM_MODEL_NAME")

    def switch_to(self, model: ModelSpec) -> None:
        if model.backend != "local":
            return
        if not model.vllm_name or not model.vllm_path:
            raise ValueError(f"Local model '{model.key}' lacks vLLM configuration.")
        if not (self.model_root / model.vllm_path).is_dir():
            raise FileNotFoundError(f"Local model assets not found: {self.model_root / model.vllm_path}")

        if self._original_env is None:
            self._original_env = self._get_deployment_env(
                "vllm", "vllm", {"LLM_MODEL_NAME", "LLM_MODEL_PATH"}
            )
            self._original_worker_env = self._get_deployment_env(
                "llm-worker", "llm-worker", {"LocalLlm__ModelName"}
            )
        configured_name, configured_path = self._configured_model()
        live_env = self._get_deployment_env("vllm", "vllm", {"LLM_MODEL_NAME", "LLM_MODEL_PATH"})
        current_name = live_env.get("LLM_MODEL_NAME", configured_name)
        current_path = live_env.get("LLM_MODEL_PATH", configured_path)
        worker_env = self._get_deployment_env(
            "llm-worker", "llm-worker", {"LocalLlm__ModelName"}
        )
        current_worker_model = worker_env.get("LocalLlm__ModelName") or self._configured_worker_model()
        if (
            current_name == model.vllm_name
            and current_path == model.vllm_path
            and current_worker_model == model.vllm_name
        ):
            self._wait_for_health()
            return

        if current_name != model.vllm_name or current_path != model.vllm_path:
            self._modified = True
            self._swap_vllm(
                model.key,
                [f"LLM_MODEL_NAME={model.vllm_name}", f"LLM_MODEL_PATH={model.vllm_path}"],
            )
        else:
            self._wait_for_health()

        if current_worker_model != model.vllm_name:
            self._run(
                "kubectl", "set", "env", "deployment/llm-worker",
                f"LocalLlm__ModelName={model.vllm_name}",
                "-n", self.namespace,
            )
            self._worker_modified = True
            self._run(
                "kubectl", "rollout", "status", "deployment/llm-worker", "-n", self.namespace,
                f"--timeout={self.rollout_timeout}",
            )

    def restore(self) -> None:
        if (not self._modified and not self._worker_modified) or self._original_env is None:
            return
        if self._modified:
            env_args = [f"{name}={value}" for name, value in self._original_env.items()]
            for name in ("LLM_MODEL_NAME", "LLM_MODEL_PATH"):
                if name not in self._original_env:
                    env_args.append(f"{name}-")
            self._swap_vllm("restore", env_args)
        if self._worker_modified:
            restore_worker_args = [
                "kubectl", "set", "env", "deployment/llm-worker", "-n", self.namespace
            ]
            for name, value in self._original_worker_env.items():
                restore_worker_args.append(f"{name}={value}")
            if "LocalLlm__ModelName" not in self._original_worker_env:
                restore_worker_args.append("LocalLlm__ModelName-")
            self._run(*restore_worker_args)
            self._run(
                "kubectl", "rollout", "status", "deployment/llm-worker", "-n", self.namespace,
                f"--timeout={self.rollout_timeout}",
            )
        self._modified = False
        self._worker_modified = False

    def _deployment_replicas(self) -> int:
        value = self._run(
            "kubectl", "get", "deployment", "vllm", "-n", self.namespace,
            "-o", "jsonpath={.spec.replicas}",
        )
        try:
            return max(1, int(value))
        except ValueError:
            return 1

    def _vllm_pods(self) -> list[str]:
        output = self._run(
            "kubectl", "get", "pods", "-n", self.namespace,
            "-l", "app.kubernetes.io/name=vllm", "-o", "name",
        )
        return [line for line in output.splitlines() if line.strip()]

    def gpu_memory_used_mb(self) -> int | None:
        try:
            output = self._run(
                "nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"
            )
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return None
        values = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
        return sum(values) if values else None

    def _wait_until(self, description: str, timeout_seconds: float, check: Callable[[], bool]) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if check():
                return True
            if time.monotonic() >= deadline:
                LOGGER.warning("Timed out after %.0fs waiting for %s.", timeout_seconds, description)
                return False
            self._sleep(3.0)

    def _health_ok(self) -> bool:
        try:
            self._run(
                "kubectl", "get", "--raw",
                f"/api/v1/namespaces/{self.namespace}/services/vllm:8080/proxy/health",
            )
            return True
        except RuntimeError:
            return False

    def _wait_for_health(self) -> None:
        self._run(
            "kubectl", "rollout", "status", "deployment/vllm", "-n", self.namespace,
            f"--timeout={self.rollout_timeout}",
        )
        if not self._wait_until("vLLM /health", self.health_timeout_seconds, self._health_ok):
            raise RuntimeError("vLLM did not report /health 200 OK after the model swap.")

    def _swap_vllm(self, label: str, env_args: list[str]) -> None:
        """Stop vLLM, wait for its pod and VRAM to be released, reconfigure, and wait for /health."""
        if self._original_replicas is None:
            self._original_replicas = self._deployment_replicas()
        started = time.monotonic()
        vram_before = self.gpu_memory_used_mb()
        self._run("kubectl", "scale", "deployment/vllm", "--replicas=0", "-n", self.namespace)
        try:
            pods_gone = self._wait_until(
                "vLLM pod termination", self.vram_release_timeout_seconds, lambda: not self._vllm_pods()
            )
            released = self._wait_until(
                f"GPU memory below {self.vram_release_threshold_mb} MiB",
                self.vram_release_timeout_seconds,
                lambda: (used := self.gpu_memory_used_mb()) is None or used <= self.vram_release_threshold_mb,
            )
            vram_released = self.gpu_memory_used_mb()
            self._run("kubectl", "set", "env", "deployment/vllm", "-n", self.namespace, *env_args)
        finally:
            self._run(
                "kubectl", "scale", "deployment/vllm",
                f"--replicas={self._original_replicas}", "-n", self.namespace,
            )
        self._wait_for_health()
        entry = {
            "target": label,
            "vram_before_mb": vram_before,
            "vram_after_release_mb": vram_released,
            "vram_after_ready_mb": self.gpu_memory_used_mb(),
            "old_pod_terminated": pods_gone,
            "vram_released_below_threshold": released,
            "swap_seconds": round(time.monotonic() - started, 1),
        }
        self.swap_log.append(entry)
        LOGGER.info("vLLM swap to %s complete: %s", label, entry)
