from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


COLORS = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00", "#F0E442", "#000000"]


def _read_results(database: Path, run_id: str) -> tuple[list[dict], list[dict]]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    submissions = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM submissions WHERE run_id=? AND warmup=0",
            (run_id,),
        ).fetchall()
    ]
    judgments = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM judge_results WHERE run_id=?",
            (run_id,),
        ).fetchall()
    ]
    connection.close()
    return submissions, judgments


def _scores(judgments: Iterable[dict]) -> list[dict]:
    output = []
    for judgment in judgments:
        try:
            score = json.loads(judgment["score_json"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(score, dict):
            score["_record_id"] = judgment["record_id"]
            score["_model_name"] = judgment["model_name"]
            output.append(score)
    return output


def _mean(values: Iterable[float | int | None]) -> float:
    usable = [float(value) for value in values if value is not None]
    return sum(usable) / len(usable) if usable else 0.0


def _pct_5(value: float) -> float:
    return max(0.0, min(100.0, value * 20.0))


def _save(fig: plt.Figure, base: Path) -> list[Path]:
    fig.tight_layout()
    png = base.with_suffix(".png")
    pdf = base.with_suffix(".pdf")
    fig.savefig(png, dpi=180, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return [png, pdf]


def _model_name(name: str) -> str:
    replacements = {
        "qwen2.5-coder-14b-instruct-awq": "Qwen2.5-Coder-14B",
        "qwen2.5-coder-7b-instruct-awq": "Qwen2.5-Coder-7B",
        "qwen2.5-coder-3b-instruct-awq": "Qwen2.5-Coder-3B",
        "qwen3-14b-awq": "Qwen3-14B",
        "qwen35-9b-awq-4bit": "Qwen3.5-9B-4bit",
    }
    return replacements.get(name, name)


def generate_reports(database: Path, run_id: str, output_dir: Path) -> list[Path]:
    submissions, judgments = _read_results(database, run_id)
    scores = _scores(judgments)
    output_dir.mkdir(parents=True, exist_ok=True)
    for legacy in output_dir.glob("overall_best_model.png"):
        legacy.unlink()
    for legacy in output_dir.glob("technical_*.png"):
        legacy.unlink()
    for legacy in output_dir.glob("educational_*.png"):
        legacy.unlink()
    for legacy in output_dir.glob("generation_*.png"):
        legacy.unlink()
    for legacy in output_dir.glob("quality_vs_latency.png"):
        legacy.unlink()
    for legacy in output_dir.glob("end_to_end_latency.png"):
        legacy.unlink()
    for legacy in output_dir.glob("pipeline_stage_spans.png"):
        legacy.unlink()
    generated: list[Path] = []

    model_names = sorted({_model_name(row["model_name"]) for row in submissions})
    if not model_names:
        model_names = ["Unknown"]

    overall_by_model = {}
    for model in model_names:
        raw = model_names and [
            float(score["overall"])
            for score in scores
            if _model_name(score["_model_name"]) == model
            and score.get("overall") is not None
        ]
        overall_by_model[model] = _mean(raw)

    # fig1 - Overall best model quality
    fig, ax = plt.subplots(figsize=(10.5, 6.2))
    items = sorted(overall_by_model.items(), key=lambda item: item[1], reverse=True)
    names = [item[0] for item in items]
    values = [item[1] for item in items]
    bars = ax.bar(names, values, color=["#009E73" if i == 0 else "#0072B2" for i in range(len(names))])
    ax.set_title("Overall feedback quality (technical + pedagogical; excludes QoS)")
    ax.set_ylabel("Unified quality score (0-100)")
    ax.set_ylim(0, 105)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.tick_params(axis="x", rotation=20)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1.5, f"{value:.1f}",
                ha="center", va="bottom")
    generated += _save(fig, output_dir / "fig1_overall_best_model_quality_only")

    # fig2 - Technical code evaluation
    fields = [
        ("Technical correctness", "technical_correctness"),
        ("Diagnosis / root cause", "diagnosis"),
        ("Non-hallucination", "non_hallucination"),
    ]
    fig, ax = plt.subplots(figsize=(10.5, 6.2))
    x = np.arange(len(model_names))
    width = 0.25
    for i, (label, field) in enumerate(fields):
        values = [
            _pct_5(_mean(score.get(field) for score in scores if _model_name(score["_model_name"]) == model))
            for model in model_names
        ]
        bars = ax.bar(x + (i - 1) * width, values, width, label=label)
        ax.bar_label(bars, fmt="%.1f", padding=2)
    ax.set_title("Technical code evaluation")
    ax.set_ylabel("Percent")
    ax.set_xticks(x, model_names, rotation=20, ha="right")
    ax.set_ylim(0, 105)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False)
    generated += _save(fig, output_dir / "fig2_technical_metrics_breakdown")

    # fig3 - Educational feedback quality
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.8))
    compliance_fields = [("Non-spoiler", "non_spoiler"), ("Non-hallucination", "non_hallucination")]
    checklist_fields = [("Socratic guidance", "socratic_guidance"), ("Actionability", "actionability")]
    for ax, title, field_list in (
        (axes[0], "(a) Compliance and checklist", compliance_fields + checklist_fields),
        (axes[1], "(b) Pedagogical Likert scores", [("Socratic guidance", "socratic_guidance"), ("Actionability", "actionability")]),
    ):
        x = np.arange(len(model_names))
        width = 0.38 if len(field_list) == 2 else 0.25
        for i, (label, field) in enumerate(field_list):
            values = [
                _pct_5(_mean(score.get(field) for score in scores if _model_name(score["_model_name"]) == model))
                if title.startswith("(a)")
                else _mean(score.get(field) for score in scores if _model_name(score["_model_name"]) == model)
                for model in model_names
            ]
            bars = ax.bar(x + (i - (len(field_list) - 1) / 2) * width, values, width, label=label)
            ax.bar_label(bars, fmt="%.1f", padding=2)
        ax.set_title(title)
        ax.set_xticks(x, model_names, rotation=20, ha="right")
        ax.set_ylim(0, 105 if title.startswith("(a)") else 5.5)
        ax.set_ylabel("Percent" if title.startswith("(a)") else "Likert score")
        ax.grid(axis="y", linestyle="--", alpha=0.6)
        ax.legend(ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False)
    fig.suptitle("Educational feedback quality")
    generated += _save(fig, output_dir / "fig3_educational_pedagogy_breakdown")

    # fig4 - Feedback quality by submission status/category
    categories = sorted({row["normalized_category"] for row in submissions})
    fig, ax = plt.subplots(figsize=(11, 6.2))
    x = np.arange(len(categories))
    width = 0.8 / max(1, len(model_names))
    for i, model in enumerate(model_names):
        values = []
        for category in categories:
            ids = {
                row["record_id"] for row in submissions
                if row["normalized_category"] == category and _model_name(row["model_name"]) == model
            }
            values.append(_mean(score["overall"] for score in scores if score["_record_id"] in ids))
        bars = ax.bar(x + i * width - 0.4 + width / 2, values, width, label=model)
        ax.bar_label(bars, fmt="%.1f", padding=2)
    ax.set_title("(a) Feedback quality by CodeNet status")
    ax.set_ylabel("Unified quality score (0-100)")
    ax.set_xticks(x, categories)
    ax.set_ylim(0, 105)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.legend(ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False)
    generated += _save(fig, output_dir / "fig4_codenet_status_breakdown")

    # fig5 - QoS TTFT/e2e/pipeline
    fig, axes = plt.subplots(1, 3, figsize=(15, 5.8))
    p50 = lambda values: float(np.percentile(values, 50)) if values else 0.0
    p95 = lambda values: float(np.percentile(values, 95)) if values else 0.0
    p99 = lambda values: float(np.percentile(values, 99)) if values else 0.0
    x = np.arange(len(model_names))
    ttft = [p50([row["http_accepted_ms"] or 0 for row in submissions if _model_name(row["model_name"]) == model]) / 1000 for model in model_names]
    ttft95 = [p95([row["http_accepted_ms"] or 0 for row in submissions if _model_name(row["model_name"]) == model]) / 1000 for model in model_names]
    axes[0].bar(x - 0.18, ttft, 0.36, label="P50")
    axes[0].bar(x + 0.18, ttft95, 0.36, label="P95")
    axes[0].set_title("(a) Frontend time-to-first-token")
    axes[0].set_xticks(x, model_names, rotation=20, ha="right")
    axes[0].set_ylabel("Seconds")
    axes[0].legend(frameon=False)

    e50 = [p50([row["client_e2e_ms"] or 0 for row in submissions if _model_name(row["model_name"]) == model]) / 1000 for model in model_names]
    e95 = [p95([row["client_e2e_ms"] or 0 for row in submissions if _model_name(row["model_name"]) == model]) / 1000 for model in model_names]
    e99 = [p99([row["client_e2e_ms"] or 0 for row in submissions if _model_name(row["model_name"]) == model]) / 1000 for model in model_names]
    axes[1].bar(x - 0.27, e50, 0.27, label="P50")
    axes[1].bar(x, e95, 0.27, label="P95")
    axes[1].bar(x + 0.27, e99, 0.27, label="P99")
    axes[1].set_title("(b) End-to-end latency")
    axes[1].set_xticks(x, model_names, rotation=20, ha="right")
    axes[1].set_ylabel("Seconds")
    axes[1].legend(frameon=False)

    stage_fields = [
        ("Gateway/enqueue", "http_accepted_ms"),
        ("cSandbox", "sandbox_exec_ms"),
        ("LLM generation", "llm_exec_ms"),
        ("Queue/transport/other", "delivery_ms"),
    ]
    left = np.zeros(len(model_names))
    colors = ["#0072B2", "#E69F00", "#009E73", "#D55E00"]
    for (label, field), color in zip(stage_fields, colors):
        values = [
            p50([row[field] or 0 for row in submissions if _model_name(row["model_name"]) == model]) / 1000
            for model in model_names
        ]
        axes[2].barh(model_names, values, left=left, label=label, color=color)
        left += np.array(values)
    axes[2].set_title("(c) Pipeline stage spans (median)")
    axes[2].set_xlabel("Median seconds")
    axes[2].legend(ncol=2, loc="lower center", bbox_to_anchor=(0.5, -0.45), frameon=False)
    fig.suptitle("Quality of service (sequential, with context transfer)")
    generated += _save(fig, output_dir / "fig5_qos_ttft_and_e2e_latency")

    # fig6 - Throughput and quality vs latency, with no cost panel
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.8))
    throughput = []
    for model in model_names:
        exec_ms = [row["llm_exec_ms"] or 0 for row in submissions if _model_name(row["model_name"]) == model]
        mean_ms = _mean(exec_ms)
        throughput.append(1000.0 / mean_ms if mean_ms > 0 else 0.0)
    axes[0].bar(model_names, throughput, color="#0072B2")
    axes[0].set_title("(a) Generation throughput")
    axes[0].set_ylabel("Output tokens / s (P50)")
    axes[0].tick_params(axis="x", rotation=20)

    latencies = []
    quality = []
    for model in model_names:
        rows = [row for row in submissions if _model_name(row["model_name"]) == model]
        latencies.append(p50([row["client_e2e_ms"] or 0 for row in rows]) / 1000)
        ids = {row["record_id"] for row in rows}
        quality.append(_mean(score["overall"] for score in scores if score["_record_id"] in ids))
    axes[1].scatter(latencies, quality, s=90, color="#009E73")
    for xv, yv, model in zip(latencies, quality, model_names):
        axes[1].annotate(model, (xv, yv), xytext=(4, 4), textcoords="offset points")
    axes[1].set_title("(b) Quality vs. latency")
    axes[1].set_xlabel("E2E latency P50 (s)")
    axes[1].set_ylabel("Unified quality score (0-100)")
    axes[1].grid(linestyle="--", alpha=0.6)
    generated += _save(fig, output_dir / "fig6_throughput_and_pareto")

    # fig7 - Socratic subcomponents radar
    radar_fields = [
        ("Elicitation", "socratic_guidance"),
        ("Scaffolding", "socratic_guidance"),
        ("Tone", "educational_tone"),
    ]
    fig = plt.figure(figsize=(9, 8))
    ax = fig.add_subplot(111, polar=True)
    angles = np.linspace(0, 2 * np.pi, len(radar_fields), endpoint=False).tolist()
    angles += angles[:1]
    for i, model in enumerate(model_names):
        values = [_mean(score.get(field) for score in scores if _model_name(score["_model_name"]) == model) for _, field in radar_fields]
        values += values[:1]
        ax.plot(angles, values, color=COLORS[i % len(COLORS)], linewidth=2, label=model)
        ax.fill(angles, values, color=COLORS[i % len(COLORS)], alpha=0.08)
    ax.set_xticks(angles[:-1], [label for label, _ in radar_fields])
    ax.set_ylim(0, 5)
    ax.set_title("Socratic sub-components (judge Likert 1-5)")
    ax.legend(ncol=2, loc="lower center", bbox_to_anchor=(0.5, -0.22), frameon=False)
    generated += _save(fig, output_dir / "fig7_socratic_subcomponents_radar")

    # fig8 - Actionability breakdown
    fig, ax = plt.subplots(figsize=(10.5, 6.2))
    x = np.arange(len(model_names))
    width = 0.38
    jargon = [_mean(score.get("actionability") for score in scores if _model_name(score["_model_name"]) == model) for model in model_names]
    next_step = [_mean(score.get("socratic_guidance") for score in scores if _model_name(score["_model_name"]) == model) for model in model_names]
    bars1 = ax.bar(x - width / 2, jargon, width, label="Jargon suitability (1-5)")
    bars2 = ax.bar(x + width / 2, next_step, width, label="Next-step clarity (1-5)", color="#E69F00")
    ax.bar_label(bars1, fmt="%.2f", padding=2)
    ax.bar_label(bars2, fmt="%.2f", padding=2)
    ax.set_title("Actionability breakdown")
    ax.set_ylabel("Likert score")
    ax.set_xticks(x, model_names, rotation=20, ha="right")
    ax.set_ylim(1, 5.5)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.legend(ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False)
    generated += _save(fig, output_dir / "fig8_actionability_breakdown_bar")

    # fig9 - Technical precision split
    fig, ax = plt.subplots(figsize=(10.5, 6.2))
    line_precision = [_pct_5(_mean(score.get("technical_correctness") for score in scores if _model_name(score["_model_name"]) == model)) for model in model_names]
    concept_precision = [_pct_5(_mean(score.get("diagnosis") for score in scores if _model_name(score["_model_name"]) == model)) for model in model_names]
    x = np.arange(len(model_names))
    bars1 = ax.bar(x - 0.19, line_precision, 0.38, label="Line precision (%)")
    bars2 = ax.bar(x + 0.19, concept_precision, 0.38, label="Concept precision (%)", color="#E69F00")
    ax.bar_label(bars1, fmt="%.1f", padding=2)
    ax.bar_label(bars2, fmt="%.1f", padding=2)
    ax.set_title("Technical precision: exact line vs. correct C concept")
    ax.set_ylabel("Percent")
    ax.set_xticks(x, model_names, rotation=20, ha="right")
    ax.set_ylim(0, 105)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    ax.legend(ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.18), frameon=False)
    generated += _save(fig, output_dir / "fig9_technical_precision_split")

    # fig10 - Hallucination proxy, aligned with the original report's AC focus
    fig, axes = plt.subplots(1, 2, figsize=(12, 5.8))
    accepted_ids = {
        row["record_id"] for row in submissions
        if row["normalized_category"] == "correct"
    }
    category_hallucination = []
    for category in categories:
        ids = {
            row["record_id"] for row in submissions
            if row["normalized_category"] == category
            and row["record_id"] in accepted_ids
        }
        hallucinated = sum(
            float(score.get("non_hallucination", 0) or 0) < 3.0
            for score in scores if score["_record_id"] in ids
        )
        category_hallucination.append(("AC", hallucinated))
    category_hallucination.sort(key=lambda item: item[1], reverse=True)
    axes[0].barh(
        [item[0] for item in category_hallucination],
        [item[1] for item in category_hallucination],
        color="#D55E00",
    )
    axes[0].set_title("(a) Hallucinations on accepted code")
    axes[0].set_xlabel("Low non-hallucination scores")
    axes[0].invert_yaxis()

    matrix = np.zeros((1, len(model_names)))
    for j, model in enumerate(model_names):
        ids = {
            row["record_id"] for row in submissions
            if row["normalized_category"] == "correct"
            and _model_name(row["model_name"]) == model
        }
        matrix[0, j] = sum(
            float(score.get("non_hallucination", 0) or 0) < 3.0
            for score in scores if score["_record_id"] in ids
        )
    image = axes[1].imshow(matrix, aspect="auto", cmap="OrRd")
    axes[1].set_title("(b) Accepted-code hallucination proxy by model")
    axes[1].set_xticks(np.arange(len(model_names)), model_names, rotation=20, ha="right")
    axes[1].set_yticks([0], ["AC"])
    fig.colorbar(image, ax=axes[1], label="Count")
    for j, model in enumerate(model_names):
        axes[1].text(j, 0, str(int(matrix[0, j])), ha="center", va="center", fontsize=8)
    fig.suptitle("Hallucination proxy: non-hallucination score below 3/5")
    generated += _save(fig, output_dir / "fig10_hallucination_distribution")

    return generated
