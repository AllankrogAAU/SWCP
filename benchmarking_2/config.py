from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


def load_benchmark_env(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.split("#", 1)[0].strip() if "#" in key else key.strip()
        value = value.split("#", 1)[0].strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_benchmark_env()

if not os.getenv("AZURE_OPENAI_ENDPOINT"):
    os.environ["AZURE_OPENAI_ENDPOINT"] = os.getenv(
        "AzureAIFoundry__Models__Gpt6Sol__Endpoint", ""
    )
if not os.getenv("AZURE_OPENAI_API_KEY"):
    os.environ["AZURE_OPENAI_API_KEY"] = os.getenv(
        "AzureAIFoundry__Models__Gpt6Sol__ApiKey", ""
    )
if not os.getenv("AZURE_OPENAI_DEPLOYMENT"):
    os.environ["AZURE_OPENAI_DEPLOYMENT"] = "gpt-6-sol"


def _discover_monitoring_service_url(service_name: str, port: int) -> str | None:
    try:
        completed = subprocess.run(
            [
                "kubectl",
                "get",
                "svc",
                service_name,
                "-n",
                "monitoring",
                "-o",
                "jsonpath={.spec.clusterIP}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        ip = completed.stdout.strip()
        if completed.returncode == 0 and ip and ip != "None":
            return f"http://{ip}:{port}"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@dataclass(frozen=True)
class ModelSpec:
    name: str
    backend: str


@dataclass
class Config:
    target_url: str = field(default_factory=lambda: os.getenv("TARGET_URL", "http://localhost"))
    data_path: Path = Path("benchmarks/submissions_data_11000.jsonl")
    dataset_db_path: Path = Path("benchmarking_2/submissions.sqlite")
    models_dir: Path = Path("apps/localLLM/models")
    results_dir: Path = Path("benchmarking_2/results")
    user_count: int = 24
    user_prefix: str = "bench2_user_"
    password: str = "BenchPass123!"
    sample_per_category: int = 50
    target_rpm: float = field(
        default_factory=lambda: float(os.getenv("BENCHMARK_RPM", "100.0"))
    )
    max_concurrency: int = field(
        default_factory=lambda: int(os.getenv("VLLM_MAX_NUM_SEQS", "10"))
    )
    judge_batch_size: int = 8
    warmup_count: int = 2
    timeout_s: float = 300.0
    seed: int = 20261118
    models: list[ModelSpec] = field(default_factory=list)
    switch_command: str | None = field(default_factory=lambda: os.getenv("MODEL_SWITCH_COMMAND"))
    prometheus_url: str | None = field(
        default_factory=lambda: os.getenv("PROMETHEUS_URL") or _discover_monitoring_service_url("prometheus", 9090)
    )
    loki_url: str | None = field(
        default_factory=lambda: os.getenv("LOKI_URL") or _discover_monitoring_service_url("loki", 3100)
    )
    judge_url: str | None = field(default_factory=lambda: os.getenv("JUDGE_URL"))
    judge_api_key: str | None = field(
        default_factory=lambda: os.getenv("JUDGE_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
    )
    judge_model: str = field(
        default_factory=lambda: os.getenv(
            "JUDGE_MODEL",
            os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-6-sol"),
        )
    )
    judge_api_version: str = field(default_factory=lambda: os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"))

    def __post_init__(self) -> None:
        self.data_path = Path(self.data_path)
        self.dataset_db_path = Path(self.dataset_db_path)
        self.results_dir = Path(self.results_dir)

    def resolve_token_cache_path(self) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        return self.results_dir / ".token_cache.json"

    @classmethod
    def default(cls) -> "Config":
        if os.getenv("BENCHMARK_MODELS"):
            return cls(models=parse_models(os.environ["BENCHMARK_MODELS"]))
        from .cluster_switch import discover_local_models
        local_names = discover_local_models(cls().models_dir)
        models = [ModelSpec(name=name, backend="local") for name in local_names]
        models.append(ModelSpec(name="GPT-6-sol", backend="azure"))
        return cls(models=models)

    def judge_endpoint(self) -> str:
        if self.judge_url:
            return self.judge_url.rstrip("/")
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        deployment = self.judge_model
        if deployment.lower().replace("-", "").replace("_", "") in {"gpt6sol"}:
            deployment = "gpt-6-sol"
        if not endpoint:
            raise RuntimeError("Set JUDGE_URL or AZURE_OPENAI_ENDPOINT for cloud judging.")
        return (
            f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions"
            f"?api-version={self.judge_api_version}"
        )


def parse_models(value: str) -> list[ModelSpec]:
    models: list[ModelSpec] = []
    for item in value.split(","):
        backend, separator, name = item.strip().partition(":")
        if not separator or backend not in {"local", "azure"} or not name:
            raise ValueError(f"Invalid model specification {item!r}; use local:name or azure:name.")
        models.append(ModelSpec(name=name, backend=backend))
    if not models:
        raise ValueError("At least one model is required.")
    return models
