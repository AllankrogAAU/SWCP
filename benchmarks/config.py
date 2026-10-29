"""Configuration dataclasses and environment discovery for SWCP benchmark suite."""

from __future__ import annotations

import os
from pathlib import Path
from pydantic import BaseModel, Field


class BenchmarkConfig(BaseModel):
    """Central configuration for benchmark runs."""
    target_url: str = Field(default_factory=lambda: os.getenv("TARGET_URL", "http://localhost"))
    prometheus_url: str | None = Field(default_factory=lambda: os.getenv("PROMETHEUS_URL"))
    loki_url: str | None = Field(default_factory=lambda: os.getenv("LOKI_URL"))
    results_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("RESULTS_DIR", "/app/results" if Path("/app/results").exists() else "./benchmarks/results")
        ).resolve()
    )
    user_pool_size: int = Field(default=50)
    user_prefix: str = "bench_user_"
    password: str = "BenchPass123!"
    assignment_id: str | None = None
    llm_backend: str = "local"
    hardware_tag: str = Field(default_factory=lambda: os.getenv("HARDWARE_TAG", "rtx5070ti-16gb"))
    suite_name: str = "custom"
    mock_llm_enabled: bool = False
    notes: str = ""

    # Submission distribution weights:
    # 40% clean passing primes, 30% compile error, 20% runtime crash/leak, 10% infinite loop
    workload_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "pass": 0.40,
            "compile_error": 0.30,
            "runtime_error": 0.20,
            "infinite_loop": 0.10,
        }
    )

    def resolve_token_cache_path(self) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        return self.results_dir / ".token_cache.json"

    def resolve_db_path(self) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        return self.results_dir / "benchmarks.sqlite"

