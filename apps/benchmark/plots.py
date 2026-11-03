"""Publication-style charts and latency/correctness summary tables."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from apps.benchmark.artifacts import percentile


def load_submission_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    records_by_id: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL record at line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                continue
            if record.get("record_type") == "submission":
                records.append(record)
                records_by_id[str(record["submission_id"])] = record
            elif record.get("record_type") == "telemetry_update":
                submission = records_by_id.get(str(record.get("submission_id")))
                if submission is None:
                    raise ValueError(f"Telemetry update at line {line_number} has no submission record.")
                submission["telemetry"] = record.get("telemetry")
    return records


def summarize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        key = (
            str(record.get("model_key", "unknown")),
            str(record.get("context_mode", "unknown")),
            str(record.get("traffic_mode", "sequential")),
        )
        groups.setdefault(key, []).append(record)

    rows: list[dict[str, Any]] = []
    for (model_key, context_mode, traffic_mode), group in sorted(groups.items()):
        e2e = [float(item["client_e2e_ms"]) for item in group if item.get("client_e2e_ms") is not None]
        ttft = [float(item["frontend_ttft_ms"]) for item in group if item.get("frontend_ttft_ms") is not None]
        status_judged = [
            item["judge"]
            for item in group
            if item.get("codenet_status") != "FIXTURE" and isinstance(item.get("judge"), dict)
        ]
        rows.append({
            "model_key": model_key,
            "context_mode": context_mode,
            "traffic_mode": traffic_mode,
            "submission_count": len(group),
            "completed_count": sum(item.get("final_status") == "COMPLETED" for item in group),
            "p50_client_e2e_ms": percentile(e2e, 50),
            "p95_client_e2e_ms": percentile(e2e, 95),
            "p99_client_e2e_ms": percentile(e2e, 99),
            "p50_frontend_ttft_ms": percentile(ttft, 50),
            "p95_frontend_ttft_ms": percentile(ttft, 95),
            "status_judge_count": len(status_judged),
            "status_match_rate": (
                sum(bool(item.get("status_match")) for item in status_judged) / len(status_judged)
                if status_judged else None
            ),
        })
    return rows


def write_summary(records_path: Path, output_dir: Path) -> list[dict[str, Any]]:
    records = load_submission_records(records_path)
    rows = summarize(records)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if rows:
        with (output_dir / "summary.csv").open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    _write_plots(records, rows, output_dir)
    return rows


def _write_plots(
    records: list[dict[str, Any]],
    summary: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("traffic_mode") != "sequential" or record.get("codenet_status") == "FIXTURE":
            continue
        key = (str(record.get("model_key", "unknown")), str(record.get("context_mode", "unknown")))
        groups.setdefault(key, []).append(record)

    labels = [f"{model}\n{context}" for model, context in groups]
    ttft_data = [
        [float(item["frontend_ttft_ms"]) for item in items if item.get("frontend_ttft_ms") is not None]
        for items in groups.values()
    ]
    e2e_data = [
        [float(item["client_e2e_ms"]) for item in items if item.get("client_e2e_ms") is not None]
        for items in groups.values()
    ]

    if any(ttft_data):
        fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.2), 5))
        ax.boxplot(ttft_data, showfliers=False)
        ax.set_xticks(range(1, len(labels) + 1), labels)
        ax.set_ylabel("Client-observed time to first streamed feedback (ms)")
        ax.set_title("First-token latency by model and context mode")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        fig.savefig(output_dir / "ttft_by_model.png", dpi=300)
        plt.close(fig)

    if any(e2e_data):
        fig, ax = plt.subplots(figsize=(max(8, len(labels) * 1.2), 5))
        ax.boxplot(e2e_data, showfliers=False)
        ax.set_xticks(range(1, len(labels) + 1), labels)
        ax.set_ylabel("Client-observed end-to-end latency (ms)")
        ax.set_title("End-to-end latency by model and context mode")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        fig.savefig(output_dir / "e2e_latency_by_model.png", dpi=300)
        plt.close(fig)

    matched = [row for row in summary if row["status_match_rate"] is not None]
    if matched:
        fig, ax = plt.subplots(figsize=(max(8, len(matched) * 1.2), 5))
        ax.bar(
            [f"{row['model_key']}\n{row['context_mode']}\n{row['traffic_mode']}" for row in matched],
            [row["status_match_rate"] for row in matched],
            color="#2878b5",
        )
        ax.set_ylim(0, 1)
        ax.set_ylabel("Observed sandbox status match rate")
        ax.set_title("CodeNet status agreement by model and context mode")
        ax.grid(axis="y", alpha=0.25)
        fig.tight_layout()
        fig.savefig(output_dir / "status_agreement.png", dpi=300)
        plt.close(fig)
