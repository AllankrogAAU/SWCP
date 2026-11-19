"""Storage manager for persisting benchmark runs, submission stage breakdowns, and metrics to SQLite and CSV/JSON."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from benchmarks.config import BenchmarkConfig

logger = logging.getLogger("benchmarks.storage")

INIT_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    suite_name TEXT,
    hardware_tag TEXT,
    started_at_utc TEXT,
    ended_at_utc TEXT,
    llm_model TEXT,
    vllm_gpu_mem_util REAL,
    vllm_max_num_seqs INTEGER,
    vllm_kv_cache_dtype TEXT,
    mock_llm_enabled INTEGER,
    arrival_mode TEXT,
    target_rate_per_min REAL,
    concurrent_users INTEGER,
    cold_start INTEGER,
    target_url TEXT,
    target_http_rtt_ms REAL,
    inter_node_rtt_ms REAL,
    clock_offset_s REAL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    run_id TEXT,
    step_label TEXT,
    user_id TEXT,
    fixture_type TEXT,
    client_e2e_ms REAL,
    client_submit_ms REAL,
    http_enqueue_ms REAL,
    submit_edge_network_ms REAL,
    sandbox_queue_wait_ms REAL,
    sandbox_exec_ms REAL,
    llm_queue_wait_ms REAL,
    llm_exec_ms REAL,
    orchestration_overhead_ms REAL,
    pipeline_total_ms REAL,
    delivery_overhead_ms REAL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    tokens_per_sec REAL,
    final_status TEXT,
    terminal_via TEXT,
    outcome TEXT,
    llm_target TEXT,
    sandbox_node TEXT,
    llm_worker_node TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS step_summaries (
    run_id TEXT,
    step_label TEXT,
    target_load REAL,
    achieved_throughput_per_min REAL,
    total_requests INTEGER,
    success_rate REAL,
    p50_e2e_ms REAL,
    p90_e2e_ms REAL,
    p95_e2e_ms REAL,
    p99_e2e_ms REAL,
    max_e2e_ms REAL,
    in_flight_end INTEGER,
    backlog_growth_per_min REAL,
    step_duration_actual_s REAL,
    is_sustained INTEGER,
    mean_enqueue_ms REAL,
    p95_submit_edge_ms REAL,
    p95_sandbox_wait_ms REAL,
    p95_sandbox_exec_ms REAL,
    p95_llm_wait_ms REAL,
    p95_llm_exec_ms REAL,
    p95_llm_exec_primary_ms REAL,
    p95_llm_exec_overflow_ms REAL,
    p95_orchestration_overhead_ms REAL,
    p95_delivery_overhead_ms REAL,
    ws_completion_rate REAL,
    peak_kv_cache_pct REAL,
    primary_kv_cache_pct REAL,
    overflow_kv_cache_pct REAL,
    mean_tok_per_s REAL,
    primary_requests INTEGER,
    overflow_requests INTEGER,
    max_replicas_sandbox INTEGER,
    max_replicas_llm_worker INTEGER,
    max_replicas_core_api INTEGER,
    max_replicas_sandbox_worker INTEGER,
    max_replicas_llm_worker_worker INTEGER,
    max_replicas_core_api_worker INTEGER,
    PRIMARY KEY (run_id, step_label),
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS timeseries_samples (
    run_id TEXT,
    timestamp_utc TEXT,
    metric_name TEXT,
    label_name TEXT,
    value REAL,
    FOREIGN KEY(run_id) REFERENCES runs(run_id)
);
"""


class BenchmarkStorage:
    def __init__(self, config: BenchmarkConfig) -> None:
        self.config = config
        self.db_path = config.resolve_db_path()
        self._init_database()

    def _init_database(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(INIT_SQL)
            self._ensure_columns(conn, "runs", {
                "target_url": "TEXT",
                "target_http_rtt_ms": "REAL",
                "inter_node_rtt_ms": "REAL",
                "clock_offset_s": "REAL",
            })
            self._ensure_columns(conn, "submissions", {
                "client_submit_ms": "REAL",
                "submit_edge_network_ms": "REAL",
                "orchestration_overhead_ms": "REAL",
                "delivery_overhead_ms": "REAL",
                "terminal_via": "TEXT",
                "llm_target": "TEXT",
                "sandbox_node": "TEXT",
                "llm_worker_node": "TEXT",
            })
            self._ensure_columns(conn, "step_summaries", {
                "max_e2e_ms": "REAL",
                "in_flight_end": "INTEGER",
                "backlog_growth_per_min": "REAL",
                "step_duration_actual_s": "REAL",
                "is_sustained": "INTEGER",
                "p95_submit_edge_ms": "REAL",
                "p95_llm_exec_primary_ms": "REAL",
                "p95_llm_exec_overflow_ms": "REAL",
                "p95_orchestration_overhead_ms": "REAL",
                "p95_delivery_overhead_ms": "REAL",
                "ws_completion_rate": "REAL",
                "primary_kv_cache_pct": "REAL",
                "overflow_kv_cache_pct": "REAL",
                "primary_requests": "INTEGER",
                "overflow_requests": "INTEGER",
                "max_replicas_sandbox_worker": "INTEGER",
                "max_replicas_llm_worker_worker": "INTEGER",
                "max_replicas_core_api_worker": "INTEGER",
            })
            conn.commit()

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, expected: dict[str, str]) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, col_type in expected.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")

    def insert_run(self, data: dict[str, Any]) -> None:
        cols = [
            "run_id", "suite_name", "hardware_tag", "started_at_utc", "ended_at_utc",
            "llm_model", "vllm_gpu_mem_util", "vllm_max_num_seqs", "vllm_kv_cache_dtype",
            "mock_llm_enabled", "arrival_mode", "target_rate_per_min", "concurrent_users",
            "cold_start", "target_url", "target_http_rtt_ms", "inter_node_rtt_ms",
            "clock_offset_s", "notes"
        ]
        placeholders = ", ".join(["?"] * len(cols))
        vals = [data.get(c) for c in cols]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"INSERT OR REPLACE INTO runs ({', '.join(cols)}) VALUES ({placeholders})", vals)
            conn.commit()

    def update_run_ended(self, run_id: str, ended_at_utc: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE runs SET ended_at_utc = ? WHERE run_id = ?", (ended_at_utc, run_id))
            conn.commit()

    def insert_submission(self, data: dict[str, Any]) -> None:
        cols = [
            "submission_id", "run_id", "step_label", "user_id", "fixture_type",
            "client_e2e_ms", "client_submit_ms", "http_enqueue_ms", "submit_edge_network_ms",
            "sandbox_queue_wait_ms", "sandbox_exec_ms", "llm_queue_wait_ms", "llm_exec_ms",
            "orchestration_overhead_ms", "pipeline_total_ms", "delivery_overhead_ms",
            "prompt_tokens", "completion_tokens", "tokens_per_sec",
            "final_status", "terminal_via", "outcome", "llm_target", "sandbox_node", "llm_worker_node"
        ]
        placeholders = ", ".join(["?"] * len(cols))
        vals = [data.get(c) for c in cols]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"INSERT OR REPLACE INTO submissions ({', '.join(cols)}) VALUES ({placeholders})", vals)
            conn.commit()

    def insert_step_summary(self, data: dict[str, Any]) -> None:
        cols = [
            "run_id", "step_label", "target_load", "achieved_throughput_per_min",
            "total_requests", "success_rate", "p50_e2e_ms", "p90_e2e_ms",
            "p95_e2e_ms", "p99_e2e_ms", "max_e2e_ms", "in_flight_end",
            "backlog_growth_per_min", "step_duration_actual_s", "is_sustained",
            "mean_enqueue_ms", "p95_submit_edge_ms",
            "p95_sandbox_wait_ms", "p95_sandbox_exec_ms", "p95_llm_wait_ms", "p95_llm_exec_ms",
            "p95_llm_exec_primary_ms", "p95_llm_exec_overflow_ms",
            "p95_orchestration_overhead_ms", "p95_delivery_overhead_ms", "ws_completion_rate",
            "peak_kv_cache_pct", "primary_kv_cache_pct", "overflow_kv_cache_pct",
            "mean_tok_per_s", "primary_requests", "overflow_requests",
            "max_replicas_sandbox", "max_replicas_llm_worker", "max_replicas_core_api",
            "max_replicas_sandbox_worker", "max_replicas_llm_worker_worker", "max_replicas_core_api_worker"
        ]
        placeholders = ", ".join(["?"] * len(cols))
        vals = [data.get(c) for c in cols]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(f"INSERT OR REPLACE INTO step_summaries ({', '.join(cols)}) VALUES ({placeholders})", vals)
            conn.commit()

    def export_artifacts(self, run_id: str, output_dir: Path | None = None) -> Path:
        """Export CSV and JSON artifacts for a specific run."""
        out = output_dir or (self.config.results_dir / run_id)
        out.mkdir(parents=True, exist_ok=True)

        with sqlite3.connect(self.db_path) as conn:
            run_df = pd.read_sql_query("SELECT * FROM runs WHERE run_id = ?", conn, params=[run_id])
            sub_df = pd.read_sql_query("SELECT * FROM submissions WHERE run_id = ?", conn, params=[run_id])
            sum_df = pd.read_sql_query("SELECT * FROM step_summaries WHERE run_id = ?", conn, params=[run_id])

        if not run_df.empty:
            run_meta = run_df.iloc[0].to_dict()
            with open(out / "run_metadata.json", "w", encoding="utf-8") as f:
                json.dump(run_meta, f, indent=2)

        sub_df.to_csv(out / "submissions.csv", index=False)
        sum_df.to_csv(out / "summary.csv", index=False)
        logger.info("Exported artifacts to %s", out)
        return out

