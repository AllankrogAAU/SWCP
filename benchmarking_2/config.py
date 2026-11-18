from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModelSpec:
    name: str
    backend: str


@dataclass
class Config:
    target_url: str = field(default_factory=lambda: os.getenv("TARGET_URL", "http://localhost"))
    data_path: Path = Path("benchmarks/submissions_data_11000.jsonl")
    dataset_db_path: Path = Path("benchmarking_2/submissions.sqlite")
    results_dir: Path = Path("benchmarking_2/results")
    user_count: int = 20
    user_prefix: str = "bench2_user_"
    password: str = "BenchPass123!"
    sample_per_category: int = 100
    judge_batch_size: int = 8
    warmup_count: int = 2
    timeout_s: float = 300.0
    seed: int = 20261118
    models: list[ModelSpec] = field(default_factory=list)
    switch_command: str | None = field(default_factory=lambda: os.getenv("MODEL_SWITCH_COMMAND"))
    prometheus_url: str | None = field(default_factory=lambda: os.getenv("PROMETHEUS_URL"))
    loki_url: str | None = field(default_factory=lambda: os.getenv("LOKI_URL"))
    judge_url: str | None = field(default_factory=lambda: os.getenv("JUDGE_URL"))
    judge_api_key: str | None = field(
        default_factory=lambda: os.getenv("JUDGE_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
    )
    judge_model: str = field(default_factory=lambda: os.getenv("JUDGE_MODEL", "GPT-6-sol"))
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
        models = os.getenv("BENCHMARK_MODELS", "local:qwen,azure:GPT-6-sol")
        return cls(models=parse_models(models))

    def judge_endpoint(self) -> str:
        if self.judge_url:
            return self.judge_url.rstrip("/")
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", self.judge_model)
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
