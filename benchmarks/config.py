"""Configuration dataclasses and environment discovery for SWCP benchmark suite."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field

DEFAULT_SUITES_CONFIG_PATH = Path(__file__).resolve().parent / "suites.toml"

SUITE_ALIASES: dict[str, str] = {
    "obj1_bottleneck": "sla_curve",
    "obj4_sla_curve": "sla_curve",
    "obj2_infinite_gpu": "infinite_gpu",
    "obj3_vllm_sweep": "vllm_sweep",
}


def load_suites_config(path: Path | None = None) -> dict[str, Any]:
    """Load suite defaults from TOML configuration file if present."""
    cfg_path = path or DEFAULT_SUITES_CONFIG_PATH
    if not cfg_path.exists():
        return {"common": {}, "suites": {}}
    with open(cfg_path, "rb") as f:
        data = tomllib.load(f)
    return {
        "common": data.get("common", {}) or {},
        "suites": data.get("suites", {}) or {},
    }


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
    inter_node_rtt_ms: float | None = Field(
        default_factory=lambda: float(os.getenv("INTER_NODE_RTT_MS")) if os.getenv("INTER_NODE_RTT_MS") else None
    )
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

