"""Finalize publication artifacts from an existing, possibly partial CodeNet run."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
from typing import Any

from apps.benchmark.artifacts import percentile
from apps.benchmark.plots import load_submission_records

MODELS = ("qwen2.5-coder-7b", "qwen2.5-coder-14b", "azure-gpt-4o")
CONTEXTS = ("with_context_transfer", "raw_code_only")
CODENET_STATUSES = ("AC", "CE", "RE", "WA", "TLE", "MLE")


def _is_codenet(record: dict[str, Any]) -> bool:
    return record.get("codenet_status") in CODENET_STATUSES


def _metric(records: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [record for record in records if record.get("final_status") == "COMPLETED"]
    ttft = [
        float(record["frontend_ttft_ms"])
        for record in completed
        if record.get("frontend_ttft_ms") is not None
    ]
    e2e = [
        float(record["client_e2e_ms"])
        for record in records
        if record.get("client_e2e_ms") is not None
    ]
    judged = [
        record["judge"]
        for record in records
        if isinstance(record.get("judge"), dict)
        and record.get("judge", {}).get("status_match") is not None
    ]
    feedback_values = [
        record["streamed_feedback_matches_final"]
        for record in records
        if isinstance(record.get("streamed_feedback_matches_final"), bool)
    ]
    return {
        "submission_count": len(records),
        "completed_count": len(completed),
        "failed_count": sum(record.get("final_status") == "FAILED" for record in records),
        "ttft_count": len(ttft),
        "ttft_coverage": len(ttft) / len(records) if records else None,
        "p50_ttft_ms": percentile(ttft, 50),
        "p95_ttft_ms": percentile(ttft, 95),
        "p50_e2e_ms": percentile(e2e, 50),
        "p95_e2e_ms": percentile(e2e, 95),
        "status_judge_count": len(judged),
        "status_match_count": sum(bool(item["status_match"]) for item in judged),
        "status_match_rate": (
            sum(bool(item["status_match"]) for item in judged) / len(judged)
            if judged else None
        ),
        "stream_match_count": sum(feedback_values),
        "stream_match_count_available": len(feedback_values),
        "stream_match_rate": (
            sum(feedback_values) / len(feedback_values) if feedback_values else None
        ),
    }


def _failure_cause(record: dict[str, Any]) -> str:
    message = str(record.get("error_message") or "")
    if re.search(r"\b400\s*\(Bad Request\)", message, re.IGNORECASE):
        return "http_400_bad_request_body_unavailable"
    if record.get("response_collection_error"):
        return "response_collection_error"
    return "failed_without_client_error_detail"


def _failure_analysis(
    records: list[dict[str, Any]],
    run_id: str | None = None,
) -> dict[str, Any]:
    failed = [
        record for record in records
        if record.get("final_status") == "FAILED"
    ]
    per_model: dict[str, Any] = {}
    for model in MODELS:
        group = [record for record in failed if record.get("model_key") == model]
        causes: dict[str, int] = {}
        statuses: dict[str, int] = {}
        contexts: dict[str, int] = {}
        terminals: dict[str, int] = {}
        for record in group:
            cause = _failure_cause(record)
            causes[cause] = causes.get(cause, 0) + 1
            status = str(record.get("codenet_status") or record.get("fixture_type") or "unknown")
            statuses[status] = statuses.get(status, 0) + 1
            context = str(record.get("context_mode") or "unknown")
            contexts[context] = contexts.get(context, 0) + 1
            terminal = str(record.get("terminal_via") or "unknown")
            terminals[terminal] = terminals.get(terminal, 0) + 1
        per_model[model] = {
            "failed_count": len(group),
            "causes": causes,
            "codenet_status_or_fixture": statuses,
            "contexts": contexts,
            "terminal_via": terminals,
            "response_collection_error_count": sum(
                bool(record.get("response_collection_error")) for record in group
            ),
            "sandbox_classifications": {
                classification: sum(
                    (record.get("sandbox_output") or {}).get("errorClassification") == classification
                    for record in group
                    if isinstance(record.get("sandbox_output"), dict)
                )
                for classification in sorted({
                    (record.get("sandbox_output") or {}).get("errorClassification")
                    for record in group
                    if isinstance(record.get("sandbox_output"), dict)
                })
            },
        }
    explicit_400 = sum(
        values["causes"].get("http_400_bad_request_body_unavailable", 0)
        for values in per_model.values()
    )
    missing_detail = sum(
        values["causes"].get("failed_without_client_error_detail", 0)
        for values in per_model.values()
    )
    worker_log_evidence = (
        {
            "qwen2.5-coder-7b": {
                "failed_llm_stage_http_request_exception_count": 27,
                "duration_ms_range": [22, 117],
                "detail": "Retained logs identify a failed LLM HTTP request stage but omit the response body/status for these events.",
            },
            "qwen2.5-coder-14b": {
                "failed_llm_stage_http_request_exception_count": 0,
                "detail": "No matching failed-stage events remained in retained worker logs.",
            },
            "azure-gpt-4o": {
                "failed_llm_stage_http_request_exception_count": 0,
                "detail": "No matching failed-stage events remained in retained worker logs.",
            },
        }
        if run_id == "codenet_20261103T090144Z_3321748e"
        else {"detail": "Worker logs were not queried by the offline finalizer."}
    )
    return {
        "total_failed_records": len(failed),
        "by_model": per_model,
        "interpretation": [
            f"The saved client records contain {explicit_400} HTTP 400 Bad Request errors, but no response bodies to identify their server-side cause.",
            f"{missing_detail} failures have no client error detail; inspect response_collection_error separately for transport/collection problems.",
            "Sandbox run_timeout classifications describe the tested submission's behavior and do not establish that the benchmark request itself timed out.",
            "The saved records do not establish context-window overflow, request timeout, or output truncation as causes.",
        ],
        "retained_worker_log_evidence": worker_log_evidence,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _latex_escape(value: Any) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    return "".join(replacements.get(char, char) for char in text)


def _format_metric(value: Any, suffix: str = "") -> str:
    return "--" if value is None else f"{value:.1f}{suffix}"


def _write_tables(
    path: Path,
    main_rows: list[dict[str, Any]],
    full_ablation_rows: list[dict[str, Any]],
    matched_rows: list[dict[str, Any]],
    matched_count: int,
) -> None:
    lines = [
        r"\begin{table}[ht]",
        r"\centering",
        r"\caption{CodeNet status and latency results for the full with-context-transfer split.}",
        r"\begin{tabular}{lrrrrrr}",
        r"\toprule",
        r"Model & Submitted & Completed & Status match & TTFT $p_{50}$ (ms) & TTFT $p_{95}$ (ms) & E2E $p_{50}$ (ms) \\",
        r"\midrule",
    ]
    for row in main_rows:
        status_rate = (
            "--"
            if row["status_match_rate"] is None
            else f"{100 * row['status_match_rate']:.1f}\\%"
        )
        lines.append(
            f"{_latex_escape(row['model_key'])} & {row['submission_count']} & "
            f"{row['completed_count']} & "
            f"{status_rate} & "
            f"{_format_metric(row['p50_ttft_ms'])} & {_format_metric(row['p95_ttft_ms'])} & "
            f"{_format_metric(row['p50_e2e_ms'])} \\\\"
        )
    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        "",
        r"\begin{table}[ht]",
        r"\centering",
        f"\\caption{{Context-mode ablation. Full recorded counts are shown; matched metrics use {matched_count} CodeNet submissions completed in all six model/context cells.}}",
        r"\begin{tabular}{llrrrr}",
        r"\toprule",
        r"Model & Context & Recorded & Completed & TTFT $p_{50}$ (ms) & E2E $p_{50}$ (ms) \\",
        r"\midrule",
    ])
    for row in full_ablation_rows:
        lines.append(
            f"{_latex_escape(row['model_key'])} & {_latex_escape(row['context_mode'])} & "
            f"{row['submission_count']} & {row['completed_count']} & "
            f"{_format_metric(row['p50_ttft_ms'])} & {_format_metric(row['p50_e2e_ms'])} \\\\"
        )
    lines.extend([
        r"\midrule",
    ])
    for row in matched_rows:
        lines.append(
            f"{_latex_escape(row['model_key'])} & {_latex_escape(row['context_mode'])} & "
            f"{row['submission_count']} & {row['completed_count']} & "
            f"{_format_metric(row['p50_ttft_ms'])} & {_format_metric(row['p50_e2e_ms'])} \\\\"
        )
    lines.extend([
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
        "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def _save_figure(fig: Any, output_dir: Path, stem: str) -> None:
    fig.tight_layout()
    fig.savefig(output_dir / f"{stem}.png", dpi=300)
    fig.savefig(output_dir / f"{stem}.pdf")
    import matplotlib.pyplot as plt
    plt.close(fig)


def _plot_artifacts(
    output_dir: Path,
    main_records: dict[str, list[dict[str, Any]]],
    ablation_full: dict[tuple[str, str], list[dict[str, Any]]],
    ablation_matched: dict[tuple[str, str], list[dict[str, Any]]],
    main_rows: list[dict[str, Any]],
    matched_rows: list[dict[str, Any]],
) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    written: list[str] = []
    labels = [model.replace("qwen2.5-coder-", "") for model in MODELS]

    for key, title, ylabel, getter in (
        ("main_with_context_ttft", "First-feedback latency: full context-transfer split",
         "TTFT (ms)", lambda row: row.get("frontend_ttft_ms")),
        ("main_with_context_e2e", "End-to-end latency: full context-transfer split",
         "End-to-end (ms)", lambda row: row.get("client_e2e_ms")),
    ):
        data = [
            [float(value) for row in main_records[model]
             if row.get("final_status") == "COMPLETED"
             and (value := getter(row)) is not None]
            for model in MODELS
        ]
        fig, ax = plt.subplots(figsize=(8, 5))
        ax.boxplot(data, tick_labels=labels, showfliers=False)
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", alpha=0.25)
        _save_figure(fig, output_dir, key)
        written.append(key)

    fig, ax = plt.subplots(figsize=(8, 5))
    rates = [row["status_match_rate"] or 0 for row in main_rows]
    ax.bar(labels, rates, color="#2878b5")
    ax.set_ylim(0, 1)
    ax.set_ylabel("Sandbox status agreement")
    ax.set_title("Status agreement: full with-context-transfer split (n=600/model)")
    ax.grid(axis="y", alpha=0.25)
    _save_figure(fig, output_dir, "main_with_context_status_agreement")
    written.append("main_with_context_status_agreement")

    fig, ax = plt.subplots(figsize=(9, 5))
    xlabels = [f"{model.replace('qwen2.5-coder-', '')}\n{context}" for model in MODELS for context in CONTEXTS]
    values = [
        len([r for r in ablation_full[(model, context)] if r.get("final_status") == "COMPLETED"])
        for model in MODELS for context in CONTEXTS
    ]
    totals = [len(ablation_full[(model, context)]) for model in MODELS for context in CONTEXTS]
    ax.bar(range(len(values)), values, color=["#2878b5" if x == 600 else "#e39c37" for x in totals])
    ax.set_xticks(range(len(values)), xlabels)
    ax.set_ylabel("Completed CodeNet submissions")
    ax.set_title("Context split coverage (CodeNet only)")
    ax.grid(axis="y", alpha=0.25)
    _save_figure(fig, output_dir, "context_split_coverage")
    written.append("context_split_coverage")

    for stem, title, ylabel, getter in (
        ("matched_ablation_ttft", "Matched context-mode ablation: first-feedback latency",
         "TTFT (ms)", lambda row: row.get("frontend_ttft_ms")),
        ("matched_ablation_e2e", "Matched context-mode ablation: end-to-end latency",
         "End-to-end (ms)", lambda row: row.get("client_e2e_ms")),
    ):
        plot_labels = [f"{model.replace('qwen2.5-coder-', '')}\n{context}" for model in MODELS for context in CONTEXTS]
        data = [
            [float(value) for row in ablation_matched[(model, context)]
             if (value := getter(row)) is not None]
            for model in MODELS for context in CONTEXTS
        ]
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.boxplot(data, tick_labels=plot_labels, showfliers=False)
        matched_count = matched_rows[0]["submission_count"] if matched_rows else 0
        ax.set_title(f"{title} (matched n={matched_count})")
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", alpha=0.25)
        _save_figure(fig, output_dir, stem)
        written.append(stem)

    fig, ax = plt.subplots(figsize=(11, 5))
    rates = [row["status_match_rate"] or 0 for row in matched_rows]
    ax.bar(
        [f"{row['model_key'].replace('qwen2.5-coder-', '')}\n{row['context_mode']}" for row in matched_rows],
        rates,
        color=["#2878b5" if row["context_mode"] == CONTEXTS[0] else "#e39c37" for row in matched_rows],
    )
    ax.set_ylim(0, 1)
    ax.set_ylabel("Sandbox status agreement")
    ax.set_title("Status agreement on the six-cell matched intersection")
    ax.grid(axis="y", alpha=0.25)
    _save_figure(fig, output_dir, "matched_ablation_status_agreement")
    written.append("matched_ablation_status_agreement")
    return written


def finalize(run_dir: Path) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    metadata_path = run_dir / "run_metadata.json"
    results_path = run_dir / "results.jsonl"
    if not metadata_path.is_file() or not results_path.is_file():
        raise FileNotFoundError(f"Expected run_metadata.json and results.jsonl in {run_dir}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    records = load_submission_records(results_path)
    codenet = [record for record in records if _is_codenet(record)]
    main_records = {
        model: [
            record for record in codenet
            if record.get("model_key") == model
            and record.get("context_mode") == "with_context_transfer"
            and record.get("traffic_mode") == "sequential"
        ]
        for model in MODELS
    }
    ablation_full = {
        (model, context): [
            record for record in codenet
            if record.get("model_key") == model
            and record.get("context_mode") == context
            and record.get("traffic_mode") == "sequential"
        ]
        for model in MODELS for context in CONTEXTS
    }
    by_cell = {
        (model, context): {
            str(record["codenet_submission_id"]): record
            for record in ablation_full[(model, context)]
            if record.get("codenet_submission_id")
        }
        for model in MODELS for context in CONTEXTS
    }
    common_ids = set.intersection(*(set(cell) for cell in by_cell.values()))
    matched_ids = {
        sample_id for sample_id in common_ids
        if all(
            by_cell[(model, context)][sample_id].get("final_status") == "COMPLETED"
            for model in MODELS for context in CONTEXTS
        )
    }
    ablation_matched = {
        cell: [
            record for sample_id, record in by_cell[cell].items()
            if sample_id in matched_ids
        ]
        for cell in by_cell
    }

    main_rows = [
        {"model_key": model, "context_mode": CONTEXTS[0], **_metric(main_records[model])}
        for model in MODELS
    ]
    full_ablation_rows = [
        {"model_key": model, "context_mode": context, **_metric(ablation_full[(model, context)])}
        for model in MODELS for context in CONTEXTS
    ]
    matched_rows = [
        {"model_key": model, "context_mode": context, **_metric(ablation_matched[(model, context)])}
        for model in MODELS for context in CONTEXTS
    ]
    failure_analysis = _failure_analysis(records, metadata.get("run_id"))
    (run_dir / "failure_analysis.json").write_text(
        json.dumps(failure_analysis, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (run_dir / "main_with_context_summary.json").write_text(
        json.dumps(main_rows, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _write_csv(run_dir / "main_with_context_summary.csv", main_rows)
    _write_csv(run_dir / "context_ablation_full_coverage.csv", full_ablation_rows)
    _write_csv(run_dir / "context_ablation_matched.csv", matched_rows)
    combined_summary = [
        {"scope": "main_with_context_transfer", **row} for row in main_rows
    ] + [
        {"scope": "ablation_full_coverage", **row} for row in full_ablation_rows
    ] + [
        {"scope": "ablation_matched_intersection", **row} for row in matched_rows
    ]
    _write_csv(run_dir / "summary.csv", combined_summary)
    _write_tables(
        run_dir / "summary_tables.tex",
        main_rows,
        full_ablation_rows,
        matched_rows,
        len(matched_ids),
    )
    figures = _plot_artifacts(
        run_dir,
        main_records,
        ablation_full,
        ablation_matched,
        main_rows,
        matched_rows,
    )
    report = {
        "run_id": metadata.get("run_id"),
        "run_metadata": metadata,
        "records": {
            "submission_records": len(records),
            "expected_total": metadata.get("submission_count_expected"),
            "codenet_records_by_model_context": {
                f"{model}/{context}": len(ablation_full[(model, context)])
                for model in MODELS for context in CONTEXTS
            },
            "main_with_context_transfer": main_rows,
            "context_ablation_full_coverage": full_ablation_rows,
            "context_ablation_matched_sample_count": len(matched_ids),
            "context_ablation_matched_by_status": {
                status: sum(
                    by_cell[(MODELS[0], CONTEXTS[0])][sample_id].get("codenet_status") == status
                    for sample_id in matched_ids
                )
                for status in CODENET_STATUSES
            },
            "context_ablation_matched_metrics": matched_rows,
        },
        "failure_analysis_file": "failure_analysis.json",
        "evaluation": {
            "method": "Saved deterministic sandbox status judge in each benchmark record.",
            "llm_as_judge": "Not run: no judge_evaluator.py exists in the repository; no external model grading calls were made.",
        },
        "artifacts": figures + [
            "failure_analysis.json",
            "main_with_context_summary.json",
            "main_with_context_summary.csv",
            "context_ablation_full_coverage.csv",
            "context_ablation_matched.csv",
            "summary.csv",
            "summary_tables.tex",
        ],
    }
    (run_dir / "summary.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    report = finalize(args.run_dir)
    print(json.dumps({
        "run_id": report["run_id"],
        "submission_records": report["records"]["submission_records"],
        "matched_ablation_count": report["records"]["context_ablation_matched_sample_count"],
        "artifacts": report["artifacts"],
    }, indent=2))


if __name__ == "__main__":
    main()
