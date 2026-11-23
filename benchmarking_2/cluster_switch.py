from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


EXCLUDED_LOCAL_MODELS = {
    "deepseek-r1-distill-qwen-7b-awq",
}


def discover_local_models(models_dir: Path) -> list[str]:
    names: list[str] = []
    if not models_dir.is_dir():
        return names
    for directory in sorted(models_dir.iterdir()):
        if not directory.is_dir():
            continue
        if not (directory / "config.json").is_file():
            continue
        served = _served_name(directory.name)
        if served in EXCLUDED_LOCAL_MODELS or directory.name.lower() in EXCLUDED_LOCAL_MODELS:
            continue
        names.append(served)
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


def _kubectl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["kubectl", *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def drain_cluster_pipeline(timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            completed = subprocess.run(
                [
                    "kubectl",
                    "exec",
                    "-n",
                    "swcp",
                    "nats-0",
                    "--",
                    "wget",
                    "-qO-",
                    "http://localhost:8222/jsz?consumers=1",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return
            payload = json.loads(completed.stdout)
            pending = 0
            for acct in payload.get("account_details", []) or []:
                for stream in acct.get("stream_detail", []) or []:
                    for consumer in stream.get("consumer_detail", []) or []:
                        pending += int(consumer.get("num_pending", 0) or 0)
                        pending += int(consumer.get("num_ack_pending", 0) or 0)
            if pending == 0:
                return
            logger.info("Waiting for cluster NATS queues to drain (%d pending/in-flight)...", pending)
        except Exception:
            return
        time.sleep(2)


def ensure_azure_worker_config() -> None:
    endpoint = (
        os.getenv("AzureAIFoundry__Models__Gpt6Sol__Endpoint")
        or os.getenv("AZURE_OPENAI_ENDPOINT")
        or ""
    ).rstrip("/")
    api_key = (
        os.getenv("AzureAIFoundry__Models__Gpt6Sol__ApiKey")
        or os.getenv("AZURE_OPENAI_API_KEY")
        or ""
    )
    need_restart = False
    try:
        current_cm = json.loads(
            _kubectl("get", "configmap", "llm-worker-config", "-n", "swcp", "-o", "json").stdout
        ).get("data", {})
        cm_updates: dict[str, str] = {}
        if current_cm.get("AzureAIFoundry__DefaultModel") != "Gpt6Sol":
            cm_updates["AzureAIFoundry__DefaultModel"] = "Gpt6Sol"
        if endpoint and current_cm.get("AzureAIFoundry__Models__Gpt6Sol__Endpoint") != endpoint:
            cm_updates["AzureAIFoundry__Models__Gpt6Sol__Endpoint"] = endpoint
        if cm_updates:
            _kubectl(
                "patch",
                "configmap",
                "llm-worker-config",
                "-n",
                "swcp",
                "--type=merge",
                "-p",
                json.dumps({"data": cm_updates}),
            )
            need_restart = True
        if api_key:
            _kubectl(
                "patch",
                "secret",
                "llm-worker-secrets",
                "-n",
                "swcp",
                "--type=merge",
                "-p",
                json.dumps({"stringData": {"AzureAIFoundry__Models__Gpt6Sol__ApiKey": api_key}}),
            )
        if need_restart:
            _kubectl("rollout", "restart", "deployment/llm-worker", "-n", "swcp")
            _kubectl("rollout", "status", "deployment/llm-worker", "-n", "swcp", "--timeout=180s")
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("Could not verify/patch Azure llm-worker configmap: %s", exc)


def switch_local_model(model_name: str, models_dir: Path, max_num_seqs: int | None = None) -> None:
    model_path, served_name = resolve_model_path(model_name, models_dir)
    if served_name != model_name:
        model_name = served_name

    seqs = max_num_seqs or int(os.getenv("VLLM_MAX_NUM_SEQS", "10"))
    overflow_threshold = int(os.getenv("VLLM_OVERFLOW_THRESHOLD", str(seqs)))
    max_ack_pending = int(os.getenv("LLM_LOCAL_MAX_ACK_PENDING", str(seqs * 2)))

    drain_cluster_pipeline(timeout_s=45.0)

    # Scale vllm-overflow to 0 during single-primary local model evaluation so
    # worker-node missing model folders do not crash-loop or serve stale models.
    try:
        overflow_reps = subprocess.run(
            [
                "kubectl",
                "get",
                "deployment",
                "vllm-overflow",
                "-n",
                "swcp",
                "-o",
                "jsonpath={.spec.replicas}",
            ],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if overflow_reps and overflow_reps != "0":
            logger.info("Scaling deployment/vllm-overflow to 0 for clean single-model evaluation.")
            _kubectl("scale", "deployment/vllm-overflow", "-n", "swcp", "--replicas=0")
    except (OSError, subprocess.CalledProcessError):
        pass

    vllm_quantization = os.getenv("VLLM_QUANTIZATION", "awq_marlin")
    vllm_kv_cache_dtype = os.getenv("VLLM_KV_CACHE_DTYPE", "fp8")
    model_cfg_path = models_dir / model_path / "config.json"
    if model_cfg_path.is_file():
        try:
            model_cfg = json.loads(model_cfg_path.read_text(encoding="utf-8"))
            quant_method = str(
                (model_cfg.get("quantization_config") or {}).get("quant_method") or ""
            ).strip()
            torch_dtype = str(model_cfg.get("torch_dtype") or "").strip()
            if quant_method == "compressed-tensors":
                vllm_quantization = "compressed-tensors"
                vllm_kv_cache_dtype = "auto"
            elif quant_method == "awq":
                vllm_quantization = "awq_marlin"
                vllm_kv_cache_dtype = "fp8"
        except Exception as exc:
            logger.warning("Could not inspect %s: %s", model_cfg_path, exc)

    vllm_patch = json.dumps({
        "data": {
            "LLM_MODEL_PATH": model_path,
            "LLM_MODEL_NAME": model_name,
            "VLLM_MAX_NUM_SEQS": str(seqs),
            "VLLM_QUANTIZATION": vllm_quantization,
            "VLLM_KV_CACHE_DTYPE": vllm_kv_cache_dtype,
        }
    })
    llm_worker_patch = json.dumps({
        "data": {
            "LocalLlm__ModelName": model_name,
            "LocalLlm__MaxConcurrent": str(seqs),
            "LocalLlm__PrimaryMaxConcurrent": str(overflow_threshold),
            "LocalLlm__MaxAckPending": str(max_ack_pending),
        }
    })

    # Check if vllm and llm-worker are already configured and ready for this model
    try:
        cur_vllm = json.loads(
            _kubectl("get", "configmap", "vllm-config", "-n", "swcp", "-o", "json").stdout
        ).get("data", {})
        cur_worker = json.loads(
            _kubectl("get", "configmap", "llm-worker-config", "-n", "swcp", "-o", "json").stdout
        ).get("data", {})
        endpoint = subprocess.run(
            [
                "kubectl", "get", "endpointslices", "-n", "swcp",
                "-l", "kubernetes.io/service-name=local-llm-server",
                "-o", "jsonpath={range .items[*].endpoints[*]}{.addresses[0]}={.conditions.ready}{'\\n'}{end}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        already_ready = any(line.endswith("=true") for line in endpoint.stdout.splitlines())
        if (
            already_ready
            and cur_vllm.get("LLM_MODEL_PATH") == model_path
            and cur_vllm.get("LLM_MODEL_NAME") == model_name
            and cur_vllm.get("VLLM_MAX_NUM_SEQS") == str(seqs)
            and cur_worker.get("LocalLlm__ModelName") == model_name
        ):
            logger.info("Local vLLM is already serving %s and ready; skipping rollout restart.", model_name)
            return
    except Exception:
        pass

    _kubectl("patch", "configmap", "vllm-config", "-n", "swcp",
             "--type=merge", "-p", vllm_patch)
    _kubectl("patch", "configmap", "llm-worker-config", "-n", "swcp",
             "--type=merge", "-p", llm_worker_patch)
    _kubectl("rollout", "restart", "deployment/vllm",
             "deployment/llm-worker", "-n", "swcp")
    _kubectl("rollout", "status", "deployment/vllm",
             "-n", "swcp", "--timeout=600s")
    _kubectl("rollout", "status", "deployment/llm-worker",
             "-n", "swcp", "--timeout=180s")

    deadline = time.monotonic() + 120
    while True:
        endpoint = subprocess.run(
            [
                "kubectl", "get", "endpointslices", "-n", "swcp",
                "-l", "kubernetes.io/service-name=local-llm-server",
                "-o", "jsonpath={range .items[*].endpoints[*]}{.addresses[0]}={.conditions.ready}{'\\n'}{end}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        ready_addresses = [
            line
            for line in endpoint.stdout.splitlines()
            if line.endswith("=true")
        ]
        if ready_addresses:
            time.sleep(5)
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Local vLLM service did not publish a ready endpoint within 120 seconds."
            )
        time.sleep(2)

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
