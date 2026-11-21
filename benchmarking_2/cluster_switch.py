from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def discover_local_models(models_dir: Path) -> list[str]:
    names: list[str] = []
    if not models_dir.is_dir():
        return names
    for directory in sorted(models_dir.iterdir()):
        if not directory.is_dir():
            continue
        if not (directory / "config.json").is_file():
            continue
        names.append(_served_name(directory.name))
    return names


def _served_name(directory_name: str) -> str:
    special_names = {
        "deepseek-r1-distill-qwen-7b-awq": "deepseek-r1-distill-qwen-7b-awq",
        "deepseek-r1-distill-llama-8b-awq": "deepseek-r1-distill-llama-8b-awq",
        "gemma-2-9b-it-awq-int4": "gemma-2-9b-it-awq-int4",
        "mistral-nemo-instruct-2407-12b-awq": "mistral-nemo-instruct-2407-awq",
        "mistral-nemo-instruct-2407-awq": "mistral-nemo-instruct-2407-awq",
        "qwen2.5-coder-3b-instruct-awq": "qwen2.5-coder-3b-instruct-awq",
        "qwen2.5-coder-7b-instruct-awq": "qwen2.5-coder-7b-instruct-awq",
        "qwen2.5-coder-14b-instruct-awq": "qwen2.5-coder-14b-instruct-awq",
        "qwen3-14b-awq": "qwen3-14b-awq",
        "qwen3.5-9b-awq-4bit": "qwen35-9b-awq-4bit",
    }
    lower_name = directory_name.strip().lower()
    if lower_name in special_names:
        return special_names[lower_name]
    name = directory_name.strip()
    name = re.sub(r"[_ ]+", "-", name)
    name = re.sub(r"-+", "-", name)
    name = name.lower()
    return name


def resolve_model_path(directory_name: str, models_dir: Path) -> tuple[str, str]:
    if not models_dir.is_dir():
        raise RuntimeError(f"Local model directory does not exist: {models_dir}")

    exact = models_dir / directory_name
    if exact.is_dir() and (exact / "config.json").is_file():
        return exact.name, _served_name(exact.name)

    wanted = _normalize(directory_name)
    candidates = [
        directory
        for directory in models_dir.iterdir()
        if directory.is_dir() and (directory / "config.json").is_file()
    ]
    for directory in candidates:
        if _normalize(directory.name) == wanted or _normalize(_served_name(directory.name)) == wanted:
            return directory.name, _served_name(directory.name)

    raise RuntimeError(
        f"No local model directory matches '{directory_name}'. "
        f"Available directories: {', '.join(sorted(path.name for path in candidates))}"
    )


def _kubectl(*arguments: str) -> None:
    subprocess.run(
        ["kubectl", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def switch_local_model(model_name: str, models_dir: Path) -> None:
    model_path, served_name = resolve_model_path(model_name, models_dir)
    if served_name != model_name:
        model_name = served_name

    vllm_patch = json.dumps({
        "data": {
            "LLM_MODEL_PATH": model_path,
            "LLM_MODEL_NAME": model_name,
        }
    })
    llm_worker_patch = json.dumps({
        "data": {
            "LocalLlm__ModelName": model_name,
        }
    })

    _kubectl("patch", "configmap", "vllm-config", "-n", "swcp",
             "--type=merge", "-p", vllm_patch)
    _kubectl("patch", "configmap", "llm-worker-config", "-n", "swcp",
             "--type=merge", "-p", llm_worker_patch)
    _kubectl("rollout", "restart", "deployment/vllm",
             "deployment/vllm-overflow", "deployment/llm-worker", "-n", "swcp")
    _kubectl("rollout", "status", "deployment/vllm",
             "-n", "swcp", "--timeout=600s")
    _kubectl("rollout", "status", "deployment/vllm-overflow",
             "-n", "swcp", "--timeout=600s")
    _kubectl("rollout", "status", "deployment/llm-worker",
             "-n", "swcp", "--timeout=180s")

    active = subprocess.run(
        ["kubectl", "get", "configmap", "vllm-config", "-n", "swcp",
         "-o", "jsonpath={.data.LLM_MODEL_NAME}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active != model_name:
        raise RuntimeError(
            f"Model switch verification failed: expected '{model_name}', found '{active}'."
        )
