"""Publication figures and tables for a CodeNet feedback-quality benchmark run.

Usage::

    python -m apps.benchmark.report benchmarks/results/<run_id> [--azure-input-usd-per-mtok X ...]

Reads ``results.jsonl``, ``judgements.jsonl`` and ``run_metadata.json`` from the run directory and
writes ``report_<UTC timestamp>/`` containing 300 DPI PDF/PNG figures plus CSV and LaTeX tables:

* fig1_overall_best_model_quality_only - unified 0-100 quality score (technical + pedagogy, no QoS)
* fig2_technical_metrics_breakdown     - bug-localisation F1, root-cause accuracy, AC hallucination rate
* fig3_educational_pedagogy_breakdown  - non-spoiler, Socratic, actionability, 5-point checklist
* fig4_codenet_status_breakdown        - quality and technical accuracy per CE/RE/WA/AC
* fig5_qos_ttft_and_e2e_latency        - TTFT P50/P95, E2E P50/P95/P99, median pipeline stage spans
* fig6_throughput_cost_and_pareto      - tokens/s, cost per 1k runs, quality-vs-latency Pareto front

Overall quality = mean of seven components, each on 0-100: F1, root-cause accuracy,
100 - hallucination rate, non-spoiler compliance, (Socratic-1)/4*100, (Actionability-1)/4*100 and the
checklist percentage. Missing components (e.g. no AC judgements) are omitted from the mean.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from apps.benchmark.artifacts import percentile
from apps.benchmark.dataset import RUBRIC_STATUSES
from apps.benchmark.judge import FAULTY_STATUSES
from apps.benchmark.model_manager import MODELS
from apps.benchmark.plots import load_submission_records

OKABE_ITO = ("#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#000000")
DPI = 300
QUALITY_COMPONENTS = (
    ("bug_localization_f1", "Bug localisation F1"),
    ("root_cause_accuracy", "Root-cause accuracy"),
    ("no_hallucination", "100 - hallucination rate"),
    ("non_spoiler", "Non-spoiler compliance"),
    ("socratic_norm", "Socratic guidance"),
    ("actionability_norm", "Actionability & clarity"),
    ("checklist", "5-point checklist"),
)


# --------------------------------------------------------------------------------------
# Loading and aggregation
# --------------------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def latest_judgements(path: Path) -> dict[str, dict[str, Any]]:
    """Last judgement per submission (re-runs append a newer entry after a judge error)."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in _load_jsonl(path):
        latest[str(entry.get("submission_id"))] = entry
    return latest


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _display(model_key: str) -> str:
    spec = MODELS.get(model_key)
    name = spec.display_name if spec else model_key
    for suffix in ("-Instruct-AWQ-INT4", "-Instruct-AWQ", "-awq", "-AWQ"):
        name = name.replace(suffix, "")
    return name


def _components(metrics_list: list[dict[str, Any]], statuses: list[str]) -> dict[str, float | None]:
    """Per-component 0-100 means for a set of judged responses."""
    f1 = [100 * m["bug_localization_f1"] for m, s in zip(metrics_list, statuses)
          if s in FAULTY_STATUSES and m.get("bug_localization_f1") is not None]
    rca = [100 * m["root_cause_diagnosis_accuracy"] for m, s in zip(metrics_list, statuses)
           if s in FAULTY_STATUSES and m.get("root_cause_diagnosis_accuracy") is not None]
    fp = [100 * float(m["false_positive_hallucination"]) for m, s in zip(metrics_list, statuses)
          if s == "AC" and m.get("false_positive_hallucination") is not None]
    return {
        "bug_localization_f1": _mean(f1),
        "root_cause_accuracy": _mean(rca),
        "hallucination_rate": _mean(fp),
        "no_hallucination": None if not fp else 100 - _mean(fp),
        "non_spoiler": _mean([100 * m["non_spoiler_compliance"] for m in metrics_list]),
        "socratic": _mean([m["socratic_guidance_score"] for m in metrics_list]),
        "actionability": _mean([m["actionability_clarity_score"] for m in metrics_list]),
        "socratic_norm": _mean([(m["socratic_guidance_score"] - 1) / 4 * 100 for m in metrics_list]),
        "actionability_norm": _mean([(m["actionability_clarity_score"] - 1) / 4 * 100 for m in metrics_list]),
        "checklist": _mean([m["checklist_percent"] for m in metrics_list]),
    }


def overall_score(components: dict[str, float | None]) -> float | None:
    values = [components[key] for key, _ in QUALITY_COMPONENTS if components.get(key) is not None]
    return _mean(values)


def _bootstrap_ci(
    metrics_list: list[dict[str, Any]],
    statuses: list[str],
    resamples: int = 1000,
    seed: int = 2026,
) -> tuple[float | None, float | None]:
    if len(metrics_list) < 2:
        return None, None
    rng = random.Random(seed)
    indices = range(len(metrics_list))
    scores: list[float] = []
    for _ in range(resamples):
        pick = [rng.choice(indices) for _ in indices]
        score = overall_score(_components([metrics_list[i] for i in pick], [statuses[i] for i in pick]))
        if score is not None:
            scores.append(score)
    if not scores:
        return None, None
    scores.sort()
    return scores[int(0.025 * len(scores))], scores[min(len(scores) - 1, int(0.975 * len(scores)))]


def _model_order(records: list[dict[str, Any]], metadata: dict[str, Any]) -> list[str]:
    order = [key for key in metadata.get("models", []) if any(r.get("model_key") == key for r in records)]
    for record in records:
        key = str(record.get("model_key"))
        if key not in order:
            order.append(key)
    return order


def quality_rows(
    records: list[dict[str, Any]],
    judgements: dict[str, dict[str, Any]],
    models: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summary: list[dict[str, Any]] = []
    by_status: list[dict[str, Any]] = []
    for model in models:
        model_records = [r for r in records if r.get("model_key") == model]
        judged = [
            (judgements[str(r["submission_id"])], str(r.get("codenet_status")))
            for r in model_records
            if judgements.get(str(r["submission_id"]), {}).get("judge_status") == "ok"
        ]
        metrics_list = [entry["metrics"] for entry, _ in judged]
        statuses = [status for _, status in judged]
        components = _components(metrics_list, statuses)
        low, high = _bootstrap_ci(metrics_list, statuses)
        sandbox = [r["judge"] for r in model_records if isinstance(r.get("judge"), dict)]
        summary.append({
            "model_key": model,
            "model": _display(model),
            "submissions": len(model_records),
            "completed": sum(r.get("final_status") == "COMPLETED" for r in model_records),
            "judged": len(judged),
            **components,
            "overall_quality": overall_score(components),
            "overall_ci_low": low,
            "overall_ci_high": high,
            "sandbox_status_agreement": (
                100 * sum(bool(item.get("status_match")) for item in sandbox) / len(sandbox) if sandbox else None
            ),
        })
        for status in RUBRIC_STATUSES:
            status_metrics = [m for m, s in zip(metrics_list, statuses) if s == status]
            status_components = _components(status_metrics, [status] * len(status_metrics))
            status_records = [r for r in model_records if r.get("codenet_status") == status]
            technical = (
                status_components["no_hallucination"] if status == "AC"
                else status_components["root_cause_accuracy"]
            )
            by_status.append({
                "model_key": model,
                "model": _display(model),
                "status": status,
                "submissions": len(status_records),
                "judged": len(status_metrics),
                "overall_quality": overall_score(status_components),
                "technical_accuracy": technical,
                "bug_localization_f1": status_components["bug_localization_f1"],
                "checklist": status_components["checklist"],
                "completion_rate": (
                    100 * sum(r.get("final_status") == "COMPLETED" for r in status_records) / len(status_records)
                    if status_records else None
                ),
            })
    return summary, by_status


def _model_gpu(metadata: dict[str, Any], model: str) -> dict[str, Any]:
    for run in metadata.get("model_runs", []):
        if run.get("model_key") == model:
            return run.get("gpu") or {}
    return {}


def qos_rows(
    records: list[dict[str, Any]],
    models: list[str],
    metadata: dict[str, Any],
    options: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in models:
        group = [r for r in records if r.get("model_key") == model and r.get("final_status") == "COMPLETED"]
        e2e = [float(r["client_e2e_ms"]) for r in group if r.get("client_e2e_ms") is not None]
        ttft = [float(r["frontend_ttft_ms"]) for r in group if r.get("frontend_ttft_ms") is not None]
        telemetry = [r.get("telemetry") or {} for r in group]
        stages = [t.get("stage_timings_ms") or {} for t in telemetry]
        sandbox = [float(s["sandbox"]) for s in stages if s.get("sandbox") is not None]
        llm = [float(s["llm"]) for s in stages if s.get("llm") is not None]
        enqueue = [float(s["submission_enqueue"]) for s in stages if s.get("submission_enqueue") is not None]
        other = [
            float(r["client_e2e_ms"]) - sum(float((r.get("telemetry") or {}).get("stage_timings_ms", {}).get(k, 0.0))
                                            for k in ("sandbox", "llm", "submission_enqueue"))
            for r in group
            if r.get("client_e2e_ms") is not None and (r.get("telemetry") or {}).get("stage_timings_ms")
        ]
        tps = [float(t["tokens_per_second"]) for t in telemetry if t.get("tokens_per_second")]
        prompt = [float(t["prompt_tokens"]) for t in telemetry if t.get("prompt_tokens") is not None]
        completion = [float(t["completion_tokens"]) for t in telemetry if t.get("completion_tokens") is not None]
        gpu = _model_gpu(metadata, model)
        spec = MODELS.get(model)
        backend = spec.backend if spec else ("azure" if model.startswith("azure") else "local")
        mean_e2e_s = (_mean(e2e) or 0.0) / 1000
        cost, cost_note = _cost_per_1k(backend, mean_e2e_s, gpu, _mean(prompt), _mean(completion), options)
        rows.append({
            "model_key": model,
            "model": _display(model),
            "backend": backend,
            "completed": len(group),
            "ttft_p50_s": _s(percentile(ttft, 50)),
            "ttft_p95_s": _s(percentile(ttft, 95)),
            "e2e_p50_s": _s(percentile(e2e, 50)),
            "e2e_p95_s": _s(percentile(e2e, 95)),
            "e2e_p99_s": _s(percentile(e2e, 99)),
            "stage_sandbox_p50_s": _s(percentile(sandbox, 50)),
            "stage_enqueue_p50_s": _s(percentile(enqueue, 50)),
            "stage_llm_p50_s": _s(percentile(llm, 50)),
            "stage_other_p50_s": _s(percentile([max(0.0, v) for v in other], 50)),
            "tokens_per_second_p50": percentile(tps, 50),
            "prompt_tokens_mean": _mean(prompt),
            "completion_tokens_mean": _mean(completion),
            "gpu_memory_peak_mb": gpu.get("memory_peak_mb") if backend == "local" else None,
            "gpu_power_mean_w": gpu.get("power_mean_watts") if backend == "local" else None,
            "cost_per_1k_runs_usd": cost,
            "cost_note": cost_note,
        })
    return rows


def _s(ms: float | None) -> float | None:
    return None if ms is None else ms / 1000


def _cost_per_1k(
    backend: str,
    mean_e2e_s: float,
    gpu: dict[str, Any],
    prompt_tokens: float | None,
    completion_tokens: float | None,
    options: dict[str, Any],
) -> tuple[float | None, str]:
    if backend == "local":
        power = gpu.get("power_mean_watts")
        hours = mean_e2e_s / 3600
        capex = options["gpu_capex_usd"] / options["gpu_lifetime_hours"] * hours
        energy = (power / 1000 * hours * options["electricity_usd_per_kwh"]) if power else 0.0
        note = "energy+amortised GPU" if power else "amortised GPU only (no power samples)"
        return 1000 * (capex + energy), note
    input_price = options.get("azure_input_usd_per_mtok")
    output_price = options.get("azure_output_usd_per_mtok")
    if input_price is None or output_price is None:
        return None, "Azure price not configured"
    if prompt_tokens is None or completion_tokens is None:
        return None, "token usage unavailable"
    return 1000 * (prompt_tokens * input_price + completion_tokens * output_price) / 1e6, "Azure token pricing"


def pareto_front(points: list[tuple[float, float]]) -> list[int]:
    """Indices of points not dominated (lower latency x and higher quality y are better)."""
    front = []
    for i, (x, y) in enumerate(points):
        dominated = any(
            (ox <= x and oy >= y) and (ox < x or oy > y) for j, (ox, oy) in enumerate(points) if j != i
        )
        if not dominated:
            front.append(i)
    return sorted(front, key=lambda i: points[i][0])


# --------------------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "--"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def write_table(rows: list[dict[str, Any]], path_stem: Path, columns: list[str], caption: str) -> None:
    with path_stem.with_suffix(".csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()) if rows else columns)
        writer.writeheader()
        writer.writerows(rows)
    escape: Callable[[str], str] = lambda text: (
        text.replace("\\", r"\textbackslash{}").replace("_", r"\_").replace("%", r"\%").replace("&", r"\&")
    )
    lines = [
        r"\begin{table}[ht]", r"\centering", r"\small",
        rf"\caption{{{escape(caption)}}}",
        rf"\label{{tab:{path_stem.name}}}",
        r"\begin{tabular}{" + "l" + "r" * (len(columns) - 1) + "}", r"\toprule",
        " & ".join(escape(column) for column in columns) + r" \\", r"\midrule",
    ]
    for row in rows:
        lines.append(" & ".join(escape(_fmt(row.get(column))) for column in columns) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}", ""]
    path_stem.with_suffix(".tex").write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------------------

def _pyplot():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9,
        "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.3,
        "grid.linestyle": "--", "axes.axisbelow": True, "legend.frameon": False, "savefig.bbox": "tight",
        "figure.constrained_layout.use": True,
    })
    return plt


def _save(fig: Any, out_dir: Path, stem: str) -> None:
    for suffix in (".pdf", ".png"):
        fig.savefig(out_dir / f"{stem}{suffix}", dpi=DPI)
    fig.clf()


def _v(value: float | None) -> float:
    return float("nan") if value is None else float(value)


def _annotate(ax: Any, bars: Any, fmt: str = "{:.1f}") -> None:
    for bar in bars:
        height = bar.get_height()
        if not math.isnan(height):
            ax.annotate(fmt.format(height), (bar.get_x() + bar.get_width() / 2, height),
                        xytext=(0, 2), textcoords="offset points", ha="center", va="bottom", fontsize=7)


def _grouped(ax: Any, labels: list[str], series: list[tuple[str, list[float | None]]], ylabel: str,
             ylim: tuple[float, float] | None = None, fmt: str = "{:.1f}") -> None:
    import numpy as np

    x = np.arange(len(labels))
    width = 0.8 / max(1, len(series))
    for index, (name, values) in enumerate(series):
        bars = ax.bar(x + (index - (len(series) - 1) / 2) * width, [_v(v) for v in values], width,
                      label=name, color=OKABE_ITO[index % len(OKABE_ITO)], edgecolor="black", linewidth=0.4)
        _annotate(ax, bars, fmt)
    ax.set_xticks(x, labels, rotation=20, ha="right")
    ax.set_ylabel(ylabel)
    if ylim:
        ax.set_ylim(*ylim)


def figure_overall(plt: Any, summary: list[dict[str, Any]], out_dir: Path) -> None:
    ranked = sorted(summary, key=lambda row: -1 if row["overall_quality"] is None else row["overall_quality"],
                    reverse=True)
    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    values = [_v(row["overall_quality"]) for row in ranked]
    errors = [
        [0 if row["overall_ci_low"] is None else row["overall_quality"] - row["overall_ci_low"] for row in ranked],
        [0 if row["overall_ci_high"] is None else row["overall_ci_high"] - row["overall_quality"] for row in ranked],
    ] if all(row["overall_quality"] is not None for row in ranked) else None
    colors = [OKABE_ITO[2] if i == 0 else OKABE_ITO[0] for i in range(len(ranked))]
    bars = ax.bar([row["model"] for row in ranked], values, color=colors, edgecolor="black", linewidth=0.4,
                  yerr=errors, capsize=3, error_kw={"linewidth": 0.8})
    for bar, row in zip(bars, ranked):
        if row["overall_quality"] is not None:
            top = row["overall_ci_high"] if row["overall_ci_high"] is not None else row["overall_quality"]
            ax.annotate(f"{row['overall_quality']:.1f}", (bar.get_x() + bar.get_width() / 2, top),
                        xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", fontsize=7)
    ax.set_ylim(0, 105)
    ax.set_ylabel("Unified quality score (0-100)")
    ax.set_title("Overall feedback quality (technical + pedagogical; excludes QoS)")
    ax.tick_params(axis="x", rotation=20)
    for label in ax.get_xticklabels():
        label.set_ha("right")
    fig.text(0.01, -0.02, "Mean of 7 components on 0-100; error bars: 95% bootstrap CI over judged responses. "
             f"n judged = {', '.join(str(r['judged']) for r in ranked)}.", fontsize=6.5)
    _save(fig, out_dir, "fig1_overall_best_model_quality_only")
    plt.close(fig)


def figure_technical(plt: Any, summary: list[dict[str, Any]], out_dir: Path) -> None:
    fig, ax = plt.subplots(figsize=(7.5, 3.6))
    _grouped(ax, [row["model"] for row in summary], [
        ("Bug localisation F1 (%)", [row["bug_localization_f1"] for row in summary]),
        ("Root-cause accuracy (%)", [row["root_cause_accuracy"] for row in summary]),
        ("Hallucination rate on AC (%) (lower is better)", [row["hallucination_rate"] for row in summary]),
    ], "Percent", (0, 110))
    ax.set_title("Technical code evaluation")
    ax.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=3)
    _save(fig, out_dir, "fig2_technical_metrics_breakdown")
    plt.close(fig)


def figure_pedagogy(plt: Any, summary: list[dict[str, Any]], out_dir: Path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 3.8), gridspec_kw={"width_ratios": [1, 1]})
    labels = [row["model"] for row in summary]
    _grouped(left, labels, [
        ("Non-spoiler compliance (%)", [row["non_spoiler"] for row in summary]),
        ("5-point checklist (%)", [row["checklist"] for row in summary]),
    ], "Percent", (0, 110))
    left.set_title("(a) Compliance and checklist")
    left.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2)
    _grouped(right, labels, [
        ("Socratic guidance (1-5)", [row["socratic"] for row in summary]),
        ("Actionability & clarity (1-5)", [row["actionability"] for row in summary]),
    ], "Likert score", (1, 5.5), "{:.2f}")
    right.set_title("(b) Pedagogical Likert scores")
    right.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2)
    fig.suptitle("Educational feedback quality", fontsize=10)
    _save(fig, out_dir, "fig3_educational_pedagogy_breakdown")
    plt.close(fig)


def figure_status(plt: Any, by_status: list[dict[str, Any]], models: list[str], out_dir: Path) -> None:
    import numpy as np

    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 3.9), gridspec_kw={"width_ratios": [1.4, 1]})
    names = [_display(model) for model in models]
    lookup = {(row["model_key"], row["status"]): row for row in by_status}
    _grouped(left, list(RUBRIC_STATUSES), [
        (name, [lookup.get((model, status), {}).get("overall_quality") for status in RUBRIC_STATUSES])
        for model, name in zip(models, names)
    ], "Unified quality score (0-100)", (0, 110))
    left.set_title("(a) Feedback quality by CodeNet status")
    left.tick_params(axis="x", rotation=0)
    left.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=min(4, len(names)))
    matrix = np.array([
        [_v(lookup.get((model, status), {}).get("technical_accuracy")) for status in RUBRIC_STATUSES]
        for model in models
    ])
    image = right.imshow(matrix, cmap="viridis", vmin=0, vmax=100, aspect="auto")
    right.grid(False)
    right.set_xticks(range(len(RUBRIC_STATUSES)), RUBRIC_STATUSES)
    right.set_yticks(range(len(names)), names)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            if not math.isnan(matrix[i, j]):
                right.text(j, i, f"{matrix[i, j]:.0f}", ha="center", va="center",
                           color="white" if matrix[i, j] < 60 else "black", fontsize=7)
    fig.colorbar(image, ax=right, fraction=0.046, pad=0.04, label="%")
    right.set_title("(b) Technical accuracy\n(root-cause for CE/RE/WA; 100-hallucination for AC)")
    _save(fig, out_dir, "fig4_codenet_status_breakdown")
    plt.close(fig)


def figure_qos(plt: Any, qos: list[dict[str, Any]], out_dir: Path) -> None:
    fig, (ax_ttft, ax_e2e, ax_stage) = plt.subplots(1, 3, figsize=(13, 4))
    labels = [row["model"] for row in qos]
    _grouped(ax_ttft, labels, [
        ("P50", [row["ttft_p50_s"] for row in qos]), ("P95", [row["ttft_p95_s"] for row in qos]),
    ], "Seconds", fmt="{:.1f}")
    ax_ttft.set_title("(a) Frontend time-to-first-token")
    ax_ttft.legend(fontsize=7)
    _grouped(ax_e2e, labels, [
        ("P50", [row["e2e_p50_s"] for row in qos]), ("P95", [row["e2e_p95_s"] for row in qos]),
        ("P99", [row["e2e_p99_s"] for row in qos]),
    ], "Seconds", fmt="{:.1f}")
    ax_e2e.set_title("(b) End-to-end latency")
    ax_e2e.legend(fontsize=7)
    bottom = [0.0] * len(qos)
    for index, (key, name) in enumerate((
        ("stage_enqueue_p50_s", "Gateway/enqueue"), ("stage_sandbox_p50_s", "cSandbox"),
        ("stage_llm_p50_s", "LLM generation"), ("stage_other_p50_s", "Queue/transport/other"),
    )):
        values = [row[key] or 0.0 for row in qos]
        ax_stage.barh(labels, values, left=bottom, label=name, color=OKABE_ITO[index], edgecolor="black",
                      linewidth=0.4)
        bottom = [b + v for b, v in zip(bottom, values)]
    ax_stage.set_xlabel("Median seconds")
    ax_stage.set_title("(c) Pipeline stage spans (median)")
    ax_stage.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.15), ncol=2)
    ax_stage.invert_yaxis()
    fig.suptitle("Quality of service (sequential, with context transfer)", fontsize=10)
    _save(fig, out_dir, "fig5_qos_ttft_and_e2e_latency")
    plt.close(fig)


def figure_throughput(plt: Any, qos: list[dict[str, Any]], summary: list[dict[str, Any]], out_dir: Path) -> None:
    fig, (ax_tps, ax_cost, ax_pareto) = plt.subplots(1, 3, figsize=(13, 4))
    labels = [row["model"] for row in qos]
    positions = list(range(len(qos)))
    tps = [row["tokens_per_second_p50"] for row in qos]
    bars = ax_tps.bar(positions, [v or 0.0 for v in tps], color=OKABE_ITO[0], edgecolor="black", linewidth=0.4)
    _annotate(ax_tps, [bar for bar, v in zip(bars, tps) if v is not None])
    ax_tps.set_ylabel("Output tokens / s (P50)")
    ax_tps.set_title("(a) Generation throughput")
    costs = [row["cost_per_1k_runs_usd"] for row in qos]
    bars = ax_cost.bar(positions, [v or 0.0 for v in costs], color=OKABE_ITO[1], edgecolor="black", linewidth=0.4)
    _annotate(ax_cost, [bar for bar, v in zip(bars, costs) if v is not None], "${:.2f}")
    top = max([v for v in costs if v is not None] or [1.0])
    for index, row in enumerate(qos):
        if row["cost_per_1k_runs_usd"] is None:
            ax_cost.text(index, top * 0.02, row["cost_note"], rotation=90, ha="center", va="bottom", fontsize=6.5)
    ax_cost.set_ylabel("USD per 1,000 feedback runs")
    ax_cost.set_title("(b) Estimated cost")
    for ax in (ax_tps, ax_cost):
        ax.set_xticks(positions, labels, rotation=25, ha="right")
    quality = {row["model_key"]: row["overall_quality"] for row in summary}
    points = [
        (row["e2e_p50_s"], quality.get(row["model_key"]), row["model"]) for row in qos
        if row["e2e_p50_s"] is not None and quality.get(row["model_key"]) is not None
    ]
    if points:
        front = pareto_front([(x, y) for x, y, _ in points])
        for index, (x, y, name) in enumerate(points):
            on_front = index in front
            ax_pareto.scatter(x, y, s=55, color=OKABE_ITO[2] if on_front else OKABE_ITO[7],
                              marker="o" if on_front else "x", zorder=3)
            ax_pareto.annotate(name, (x, y), xytext=(4, 4), textcoords="offset points", fontsize=7)
        ax_pareto.plot([points[i][0] for i in front], [points[i][1] for i in front], color=OKABE_ITO[2],
                       linestyle="--", linewidth=1, label="Pareto frontier")
        ax_pareto.legend(fontsize=7)
    ax_pareto.set_xlabel("E2E latency P50 (s)")
    ax_pareto.set_ylabel("Unified quality score (0-100)")
    ax_pareto.set_title("(c) Quality vs. latency")
    _save(fig, out_dir, "fig6_throughput_cost_and_pareto")
    plt.close(fig)


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------

DEFAULT_OPTIONS = {
    "electricity_usd_per_kwh": 0.30,
    "gpu_capex_usd": 900.0,
    "gpu_lifetime_hours": 26_280.0,
    "azure_input_usd_per_mtok": None,
    "azure_output_usd_per_mtok": None,
}


def generate_report(run_dir: Path, options: dict[str, Any] | None = None, output_dir: Path | None = None) -> Path:
    options = {**DEFAULT_OPTIONS, **{k: v for k, v in (options or {}).items() if v is not None}}
    run_dir = Path(run_dir)
    metadata_path = run_dir / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
    records = [
        r for r in load_submission_records(run_dir / "results.jsonl")
        if r.get("traffic_mode") == "sequential" and r.get("codenet_status") in RUBRIC_STATUSES
        and r.get("context_mode", "with_context_transfer") == "with_context_transfer"
    ]
    if not records:
        raise RuntimeError(f"No sequential CodeNet records found in {run_dir / 'results.jsonl'}.")
    judgements = latest_judgements(run_dir / "judgements.jsonl")
    models = _model_order(records, metadata)
    summary, by_status = quality_rows(records, judgements, models)
    qos = qos_rows(records, models, metadata, options)

    out_dir = output_dir or run_dir / f"report_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_table(summary, out_dir / "table_quality_summary", [
        "model", "judged", "bug_localization_f1", "root_cause_accuracy", "hallucination_rate", "non_spoiler",
        "socratic", "actionability", "checklist", "overall_quality",
    ], "Feedback quality per model (percent unless Likert 1-5).")
    write_table(by_status, out_dir / "table_status_breakdown", [
        "model", "status", "judged", "overall_quality", "technical_accuracy", "bug_localization_f1", "checklist",
    ], "Feedback quality by CodeNet status.")
    write_table(qos, out_dir / "table_qos_summary", [
        "model", "completed", "ttft_p50_s", "ttft_p95_s", "e2e_p50_s", "e2e_p95_s", "e2e_p99_s",
        "tokens_per_second_p50", "gpu_memory_peak_mb", "cost_per_1k_runs_usd",
    ], "Quality of service, throughput and estimated cost per model.")

    plt = _pyplot()
    figure_overall(plt, summary, out_dir)
    figure_technical(plt, summary, out_dir)
    figure_pedagogy(plt, summary, out_dir)
    figure_status(plt, by_status, models, out_dir)
    figure_qos(plt, qos, out_dir)
    figure_throughput(plt, qos, summary, out_dir)

    judge_config_path = run_dir / "judge_config.json"
    judge_counts: dict[str, int] = {}
    for entry in judgements.values():
        judge_counts[str(entry.get("judge_status"))] = judge_counts.get(str(entry.get("judge_status")), 0) + 1
    (out_dir / "report_metadata.json").write_text(json.dumps({
        "run_id": metadata.get("run_id", run_dir.name),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "record_count": len(records),
        "judgement_status_counts": judge_counts,
        "judge": (
            {k: v for k, v in json.loads(judge_config_path.read_text(encoding="utf-8")).items()
             if k not in {"schema", "system_prompt"}}
            if judge_config_path.is_file() else None
        ),
        "cost_assumptions": options,
        "overall_quality_definition": [label for _, label in QUALITY_COMPONENTS],
        "median_completed_per_model": statistics.median([row["completed"] for row in qos]) if qos else 0,
    }, indent=2) + "\n", encoding="utf-8")
    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--electricity-usd-per-kwh", type=float, default=DEFAULT_OPTIONS["electricity_usd_per_kwh"])
    parser.add_argument("--gpu-capex-usd", type=float, default=DEFAULT_OPTIONS["gpu_capex_usd"])
    parser.add_argument("--gpu-lifetime-hours", type=float, default=DEFAULT_OPTIONS["gpu_lifetime_hours"])
    parser.add_argument("--azure-input-usd-per-mtok", type=float)
    parser.add_argument("--azure-output-usd-per-mtok", type=float)
    args = parser.parse_args()
    print(generate_report(args.run_dir, {
        "electricity_usd_per_kwh": args.electricity_usd_per_kwh,
        "gpu_capex_usd": args.gpu_capex_usd,
        "gpu_lifetime_hours": args.gpu_lifetime_hours,
        "azure_input_usd_per_mtok": args.azure_input_usd_per_mtok,
        "azure_output_usd_per_mtok": args.azure_output_usd_per_mtok,
    }, args.output_dir))


if __name__ == "__main__":
    main()
