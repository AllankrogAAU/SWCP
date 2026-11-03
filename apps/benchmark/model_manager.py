"""Serial, rollback-safe model selection for the single-GPU vLLM deployment."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class ModelSpec:
    key: str
    backend: str
    display_name: str
    vllm_name: str | None = None
    vllm_path: str | None = None
    azure_model_key: str | None = None


MODELS = {
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
    "azure-gpt-4o": ModelSpec(
        "azure-gpt-4o",
        "azure",
        "Azure GPT-4o",
        azure_model_key="Gpt4o",
    ),
}


class LocalModelManager:
    def __init__(
        self,
        namespace: str = "swcp",
        model_root: Path | None = None,
        rollout_timeout: str = "15m",
    ) -> None:
        self.namespace = namespace
        self.model_root = model_root or Path(__file__).resolve().parents[1] / "localLLM" / "models"
        self.rollout_timeout = rollout_timeout
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
        current_name = self._original_env.get("LLM_MODEL_NAME", configured_name)
        current_path = self._original_env.get("LLM_MODEL_PATH", configured_path)
        worker_env = self._get_deployment_env(
            "llm-worker", "llm-worker", {"LocalLlm__ModelName"}
        )
        current_worker_model = worker_env.get("LocalLlm__ModelName") or self._configured_worker_model()
        if (
            current_name == model.vllm_name
            and current_path == model.vllm_path
            and current_worker_model == model.vllm_name
            and not self._modified
            and not self._worker_modified
        ):
            self._run(
                "kubectl", "rollout", "status", "deployment/vllm", "-n", self.namespace,
                f"--timeout={self.rollout_timeout}",
            )
            return

        if current_name != model.vllm_name or current_path != model.vllm_path:
            self._run(
                "kubectl", "set", "env", "deployment/vllm",
                f"LLM_MODEL_NAME={model.vllm_name}",
                f"LLM_MODEL_PATH={model.vllm_path}",
                "-n", self.namespace,
            )
            self._modified = True
            self._run(
                "kubectl", "rollout", "status", "deployment/vllm", "-n", self.namespace,
                f"--timeout={self.rollout_timeout}",
            )

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
            restore_args = ["kubectl", "set", "env", "deployment/vllm", "-n", self.namespace]
            for name, value in self._original_env.items():
                restore_args.append(f"{name}={value}")
            for name in ("LLM_MODEL_NAME", "LLM_MODEL_PATH"):
                if name not in self._original_env:
                    restore_args.append(f"{name}-")
            self._run(*restore_args)
            self._run(
                "kubectl", "rollout", "status", "deployment/vllm", "-n", self.namespace,
                f"--timeout={self.rollout_timeout}",
            )
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
