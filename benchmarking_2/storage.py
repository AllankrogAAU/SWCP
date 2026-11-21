from __future__ import annotations

import json
import sqlite3
import csv
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, mode TEXT NOT NULL, started_at TEXT NOT NULL,
  ended_at TEXT, dataset_hash TEXT NOT NULL, sample_per_category INTEGER NOT NULL,
  judge_enabled INTEGER NOT NULL, config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  run_id TEXT NOT NULL, model_name TEXT NOT NULL, backend TEXT NOT NULL,
  record_id INTEGER NOT NULL, assignment_title TEXT NOT NULL, category TEXT NOT NULL,
  normalized_category TEXT NOT NULL, submission_id TEXT, status TEXT NOT NULL,
  feedback TEXT, sandbox_output TEXT, error TEXT, client_e2e_ms REAL,
  http_accepted_ms REAL, pipeline_total_ms REAL, sandbox_queue_ms REAL,
  sandbox_exec_ms REAL, llm_queue_ms REAL, llm_exec_ms REAL,
  orchestration_ms REAL, delivery_ms REAL, confidence_json TEXT,
  sandbox_acquire_ms REAL, sandbox_acquire_source TEXT,
  sandbox_compile_ms REAL, sandbox_run_ms REAL,
  sandbox_backend TEXT, sandbox_runtime TEXT,
  sandbox_worker_node TEXT, sandbox_worker_pod TEXT,
  warmup INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, model_name, record_id)
);
CREATE TABLE IF NOT EXISTS judge_results (
  run_id TEXT NOT NULL, model_name TEXT NOT NULL, record_id INTEGER NOT NULL,
  score_json TEXT NOT NULL, PRIMARY KEY (run_id, model_name, record_id)
);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.executescript(SCHEMA)
            existing = {
                row[1]: row[2]
                for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
            }
            new_columns = {
                "sandbox_acquire_ms": "REAL",
                "sandbox_acquire_source": "TEXT",
                "sandbox_compile_ms": "REAL",
                "sandbox_run_ms": "REAL",
                "sandbox_backend": "TEXT",
                "sandbox_runtime": "TEXT",
                "sandbox_worker_node": "TEXT",
                "sandbox_worker_pod": "TEXT",
            }
            for column, column_type in new_columns.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE submissions ADD COLUMN {column} {column_type}"
                    )
            connection.commit()

    def start_run(self, run_id: str, mode: str, dataset_hash: str, sample: int, judge: bool, config: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, datetime('now'), NULL, ?, ?, ?, ?)",
                (run_id, mode, dataset_hash, sample, int(judge), json.dumps(config, default=str)),
            )

    def finish_run(self, run_id: str) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE runs SET ended_at=datetime('now') WHERE run_id=?", (run_id,))

    def submission(self, run_id: str, model: str, backend: str, item: dict[str, Any]) -> None:
        columns = (
            "run_id,model_name,backend,record_id,assignment_title,category,normalized_category,"
            "submission_id,status,feedback,sandbox_output,error,client_e2e_ms,http_accepted_ms,"
            "pipeline_total_ms,sandbox_queue_ms,sandbox_exec_ms,llm_queue_ms,llm_exec_ms,"
            "orchestration_ms,delivery_ms,confidence_json,"
            "sandbox_acquire_ms,sandbox_acquire_source,sandbox_compile_ms,sandbox_run_ms,"
            "sandbox_backend,sandbox_runtime,sandbox_worker_node,sandbox_worker_pod,warmup"
        )
        values = (
            run_id, model, backend, item["record_id"], item["assignment_title"], item["category"],
            item["normalized_category"], item.get("submission_id"), item["status"], item.get("feedback"),
            item.get("sandbox_output"), item.get("error"), item.get("client_e2e_ms"),
            item.get("http_accepted_ms"), item.get("pipeline_total_ms"), item.get("sandbox_queue_ms"),
            item.get("sandbox_exec_ms"), item.get("llm_queue_ms"), item.get("llm_exec_ms"),
            item.get("orchestration_ms"), item.get("delivery_ms"), item.get("confidence_json"),
            item.get("sandbox_acquire_ms"), item.get("sandbox_acquire_source"),
            item.get("sandbox_compile_ms"), item.get("sandbox_run_ms"),
            item.get("sandbox_backend"), item.get("sandbox_runtime"),
            item.get("sandbox_worker_node"), item.get("sandbox_worker_pod"),
            int(item.get("warmup", False)),
        )
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO submissions ({columns}) VALUES ({','.join('?' for _ in values)})",
                values,
            )

    def judge(self, run_id: str, model: str, record_id: int, score: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO judge_results VALUES (?, ?, ?, ?)",
                (run_id, model, record_id, json.dumps(score)),
            )

    def export(self, run_id: str) -> Path:
        output = self.path.with_suffix("")
        output.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            for table, filename in (
                ("runs", "run.json"),
                ("submissions", "submissions.csv"),
                ("judge_results", "judge_results.csv"),
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table}" + (" WHERE run_id=?" if table != "runs" else " WHERE run_id=?"),
                    (run_id,),
                ).fetchall()
                if table == "runs":
                    output.joinpath(filename).write_text(
                        json.dumps(dict(rows[0]) if rows else {}, indent=2), encoding="utf-8"
                    )
                else:
                    with output.joinpath(filename).open("w", newline="", encoding="utf-8") as target:
                        writer = csv.writer(target)
                        writer.writerow(rows[0].keys() if rows else [])
                        writer.writerows(rows)
        return output
