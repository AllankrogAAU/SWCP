"""Publication figures (300 DPI PDF+PNG) and LaTeX tables for the SWCP benchmark.

Inputs (apps/benchmark/logs): judged_logs.jsonl (falls back to inference_logs.jsonl),
burst_logs.jsonl. Outputs: apps/benchmark/results/graphs and apps/benchmark/results/tables.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "apps" / "benchmark" / "logs"
RESULTS = ROOT / "apps" / "benchmark" / "results"
SLA_S = 15.0
STATUSES = ("CE", "RE", "WA", "TLE_MLE", "AC")
STATUS_LABELS = {"CE": "Compile Error", "RE": "Runtime Error", "WA": "Wrong Answer",
                 "TLE_MLE": "TLE/MLE", "AC": "Accepted", "Overall": "Overall"}
LOCAL_CAPACITY = 4  # vLLM --max-num-seqs

plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.3, "savefig.bbox": "tight"})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def latest(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("run_key"):
            by_key[str(row["run_key"])] = row
    return list(by_key.values())


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))]


def label(row: dict[str, Any]) -> str:
    return f"{row.get('model_name')} ({row.get('provider')})"


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _save(fig: Any, out_dir: Path, name: str) -> list[Path]:
    paths = [out_dir / f"{name}.pdf", out_dir / f"{name}.png"]
    for path in paths:
        fig.savefig(path, dpi=300)
    plt.close(fig)
    return paths


def summarize(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("context_mode", "frontend_submit") == "frontend_submit":
            groups[label(row)].append(row)
    stats: dict[str, dict[str, Any]] = {}
    for model, records in groups.items():
        done = [r for r in records if r.get("status") == "COMPLETED"]
        e2e = [v / 1000 for r in done if (v := _num(r.get("e2e_total_latency_ms"))) is not None]
        scores = [v for r in done if (v := _num(r.get("overall_score"))) is not None]
        costs = [v for r in done if (v := _num(r.get("estimated_cost_usd"))) is not None]
        stats[model] = {
            "provider": records[0].get("provider"),
            "n": len(records),
            "success_rate": 100 * len(done) / len(records) if records else None,
            "quality": mean(scores) if scores else None,
            "p50_s": median(e2e) if e2e else None,
            "p95_s": percentile(e2e, 0.95),
            "ttft_s": (lambda xs: median(xs) if xs else None)(
                [v / 1000 for r in done if (v := _num(r.get("frontend_ttft_ms"))) is not None]),
            "tokens_per_sec": (lambda xs: median(xs) if xs else None)(
                [v for r in done if (v := _num(r.get("tokens_per_sec"))) is not None]),
            "peak_vram_mb": max([v for r in done if (v := _num(r.get("peak_vram_mb"))) is not None], default=None),
            "cost_per_10k": mean(costs) * 10_000 if costs else None,
            "sla_percent": 100 * sum(not r.get("exceeded_15s_sla") for r in done) / len(done) if done else None,
            "leak_percent": 100 * sum(bool(r.get("leaked_solution_code")) for r in done) / len(done) if done else None,
            "by_status": {
                status: (lambda xs: mean(xs) if xs else None)(
                    [v for r in done if r.get("codenet_status") == status and (v := _num(r.get("overall_score"))) is not None])
                for status in STATUSES
            },
        }
    return dict(sorted(stats.items(), key=lambda item: (item[1]["provider"] != "vLLM", item[0])))


def fig1(stats: dict[str, dict[str, Any]], out: Path) -> list[Path]:
    fig, ax = plt.subplots(figsize=(6, 4))
    for model, s in stats.items():
        if s["p95_s"] is None or s["quality"] is None:
            continue
        marker = "o" if s["provider"] == "vLLM" else "s"
        ax.scatter(s["p95_s"], s["quality"], marker=marker, s=60)
        ax.annotate(model, (s["p95_s"], s["quality"]), textcoords="offset points", xytext=(5, 4), fontsize=7)
    ax.axvline(SLA_S, color="red", linestyle="--", label="15 s SLA")
    ax.set_xlabel("Frontend P95 end-to-end latency (s)")
    ax.set_ylabel("Overall feedback quality (%)")
    ax.set_title("Quality vs. user-perceived P95 latency")
    ax.legend(loc="lower right")
    return _save(fig, out, "fig1_e2e_frontend_latency_vs_quality")


def fig2(stats: dict[str, dict[str, Any]], out: Path) -> list[Path]:
    columns = [*STATUSES, "Overall"]
    models = list(stats)
    width = 0.8 / max(1, len(models))
    x = np.arange(len(columns))
    fig, ax = plt.subplots(figsize=(9, 4))
    for index, model in enumerate(models):
        values = [stats[model]["by_status"].get(c) if c != "Overall" else stats[model]["quality"] for c in columns]
        ax.bar(x + index * width - 0.4 + width / 2, [v if v is not None else 0 for v in values], width, label=model)
    ax.set_xticks(x, [STATUS_LABELS[c] for c in columns])
    ax.set_ylabel("Overall quality score (%)")
    ax.set_ylim(0, 100)
    ax.set_title("Feedback quality by CodeNet status")
    ax.legend(fontsize=6, ncol=2, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    return _save(fig, out, "fig2_codenet_status_and_overall_score")


def fig3(rows: list[dict[str, Any]], stats: dict[str, dict[str, Any]], out: Path) -> list[Path]:
    parts = {
        "Frontend/Network": lambda r, b: _num(r.get("frontend_network_ms")),
        "Sandbox (queue + exec)": lambda r, b: _sum(b.get("sandbox_queue"), b.get("sandbox_execution")),
        "LLM (queue + inference)": lambda r, b: _sum(b.get("llm_queue"), b.get("llm_inference")),
        "Gateway WebSocket delivery": lambda r, b: _sum(r.get("gateway_websocket_delivery_ms"),
                                                        r.get("frontend_render_after_ws_ms")),
    }
    medians: dict[str, list[float]] = {}
    for model in stats:
        done = [r for r in rows if label(r) == model and r.get("status") == "COMPLETED"
                and r.get("context_mode", "frontend_submit") == "frontend_submit"]
        medians[model] = []
        for fn in parts.values():
            values = [v for r in done if (v := fn(r, r.get("pipeline_breakdown_ms") or {})) is not None]
            medians[model].append(median(values) / 1000 if values else 0.0)
    fig, ax = plt.subplots(figsize=(8, 4))
    bottom = np.zeros(len(medians))
    for index, name in enumerate(parts):
        values = np.array([medians[m][index] for m in medians])
        ax.bar(list(medians), values, bottom=bottom, label=name)
        bottom += values
    ax.axhline(SLA_S, color="red", linestyle="--", linewidth=1)
    ax.set_ylabel("Median latency (s)")
    ax.set_title("End-to-end pipeline latency breakdown")
    ax.tick_params(axis="x", rotation=30, labelsize=7)
    ax.legend(fontsize=7)
    return _save(fig, out, "fig3_pipeline_latency_breakdown_stacked_bar")


def _sum(*values: Any) -> float | None:
    numbers = [v for value in values if (v := _num(value)) is not None]
    return sum(numbers) if numbers else None


def fig4(rows: list[dict[str, Any]], stats: dict[str, dict[str, Any]], out: Path) -> tuple[list[Path], dict[str, list[float | None]]]:
    columns = ["CE", "RE", "WA", "TLE_MLE", "AC false-positive"]
    matrix: dict[str, list[float | None]] = {}
    for model in stats:
        done = [r for r in rows if label(r) == model and r.get("status") == "COMPLETED"
                and "detected_defect_correctly" in r]
        values: list[float | None] = []
        for status in STATUSES[:4]:
            subset = [r for r in done if r.get("codenet_status") == status]
            values.append(100 * sum(bool(r["detected_defect_correctly"]) for r in subset) / len(subset) if subset else None)
        ac = [r for r in done if r.get("codenet_status") == "AC"]
        values.append(100 * sum(bool(r.get("false_positive_on_correct_code")) for r in ac) / len(ac) if ac else None)
        matrix[model] = values
    data = np.array([[np.nan if v is None else v for v in row] for row in matrix.values()]) if matrix else np.zeros((0, 5))
    fig, ax = plt.subplots(figsize=(7, 0.45 * max(2, len(matrix)) + 1.2))
    image = ax.imshow(data, cmap="RdYlGn", vmin=0, vmax=100, aspect="auto")
    if matrix:
        image_fp = data.copy()
        image_fp[:, :4] = np.nan
        ax.imshow(image_fp, cmap="RdYlGn_r", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(columns)), [STATUS_LABELS.get(c, c) for c in columns], fontsize=7)
    ax.set_yticks(range(len(matrix)), list(matrix), fontsize=7)
    ax.grid(False)
    for (i, j), value in np.ndenumerate(data):
        ax.text(j, i, "–" if np.isnan(value) else f"{value:.0f}%", ha="center", va="center", fontsize=7)
    fig.colorbar(image, ax=ax, label="Diagnosis accuracy (%)")
    ax.set_title("Bug diagnosis accuracy and false positives on Accepted code")
    return _save(fig, out, "fig4_diagnosis_accuracy_heatmap"), matrix


def _burst_latencies(burst: list[dict[str, Any]]) -> dict[str, dict[int, list[float]]]:
    result: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in burst:
        level = row.get("burst_level")
        value = _num(row.get("e2e_total_latency_ms"))
        if isinstance(level, int) and row.get("status") == "COMPLETED" and value is not None:
            result[label(row)][level].append(value / 1000)
    return result


def best_models(stats: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    def key(item: tuple[str, dict[str, Any]]) -> tuple[float, float]:
        s = item[1]
        return (s["quality"] if s["quality"] is not None else -1, -(s["p95_s"] or math.inf))
    local = [item for item in stats.items() if item[1]["provider"] == "vLLM" and item[1]["quality"] is not None]
    azure = [item for item in stats.items() if item[1]["provider"] == "Azure" and item[1]["quality"] is not None]
    if not local:
        local = [item for item in stats.items() if item[1]["provider"] == "vLLM" and item[1]["p95_s"] is not None]
    if not azure:
        azure = [item for item in stats.items() if item[1]["provider"] == "Azure" and item[1]["p95_s"] is not None]
    return (max(local, key=key)[0] if local else None, max(azure, key=key)[0] if azure else None)


def fig5(burst: list[dict[str, Any]], stats: dict[str, dict[str, Any]], out: Path) -> list[Path]:
    curves = _burst_latencies(burst)
    fig, ax = plt.subplots(figsize=(6.5, 4))
    for model, levels in sorted(curves.items()):
        xs = sorted(levels)
        style = "-o" if stats.get(model, {}).get("provider", "vLLM") == "vLLM" else "--s"
        ax.plot(xs, [percentile(levels[x], 0.95) for x in xs], style, label=model, markersize=4)
    best_local, best_azure = best_models(stats)
    if best_local in curves and best_azure in curves:
        xs = sorted(set(curves[best_local]) & set(curves[best_azure]))
        hybrid = []
        for level in xs:
            local_part = sorted(curves[best_local][level])[: min(level, LOCAL_CAPACITY)]
            azure_part = sorted(curves[best_azure][level])[: max(0, level - LOCAL_CAPACITY)]
            hybrid.append(percentile(local_part + azure_part, 0.95))
        ax.plot(xs, hybrid, "-^", color="black", linewidth=2,
                label=f"Hybrid router (simulated: ≤{LOCAL_CAPACITY} local, overflow → Azure)")
    ax.axhline(SLA_S, color="red", linestyle="--", linewidth=1, label="15 s SLA")
    ax.set_xlabel("Concurrent frontend submissions (N)")
    ax.set_ylabel("Frontend P95 latency (s)")
    ax.set_title("Burst concurrency latency")
    ax.legend(fontsize=6)
    return _save(fig, out, "fig5_burst_concurrency_curves")


def hybrid_pareto(rows: list[dict[str, Any]], stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Local-first router: escalate to Azure when local fails or exceeds a latency budget τ."""
    best_local, best_azure = best_models(stats)
    if not best_local or not best_azure:
        return []
    by_sample: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if row.get("context_mode", "frontend_submit") == "frontend_submit" and label(row) in (best_local, best_azure):
            by_sample[str(row.get("sample_id"))][label(row)] = row
    pairs = [(s[best_local], s[best_azure]) for s in by_sample.values()
             if best_local in s and best_azure in s and _num(s[best_azure].get("overall_score")) is not None]
    points = []
    for budget in [0, 2, 4, 6, 8, 10, 12, 15, 20, 30, 60, math.inf]:
        quality, cost, escalated = [], [], 0
        for local_row, azure_row in pairs:
            local_ok = local_row.get("status") == "COMPLETED" and _num(local_row.get("overall_score")) is not None
            local_latency = (_num(local_row.get("e2e_total_latency_ms")) or math.inf) / 1000
            if local_ok and local_latency <= budget:
                quality.append(float(local_row["overall_score"]))
                cost.append(_num(local_row.get("estimated_cost_usd")) or 0.0)
            else:
                escalated += 1
                quality.append(float(azure_row["overall_score"]))
                cost.append((_num(local_row.get("estimated_cost_usd")) or 0.0) + (_num(azure_row.get("estimated_cost_usd")) or 0.0))
        if pairs:
            points.append({"budget_s": budget, "quality": mean(quality), "cost_per_10k": mean(cost) * 10_000,
                           "escalation_rate": 100 * escalated / len(pairs)})
    return points


def fig6(rows: list[dict[str, Any]], stats: dict[str, dict[str, Any]], out: Path) -> tuple[list[Path], list[dict[str, Any]]]:
    points = hybrid_pareto(rows, stats)
    fig, ax = plt.subplots(figsize=(6, 4))
    for model, s in stats.items():
        if s["quality"] is not None and s["cost_per_10k"] is not None:
            ax.scatter(s["cost_per_10k"], s["quality"], marker="o" if s["provider"] == "vLLM" else "s", s=40)
            ax.annotate(model, (s["cost_per_10k"], s["quality"]), textcoords="offset points", xytext=(4, 3), fontsize=6)
    if points:
        ax.plot([p["cost_per_10k"] for p in points], [p["quality"] for p in points], "-^", color="black",
                label="Local-first hybrid (escalate on failure / latency budget τ)")
        for p in points:
            if p["budget_s"] in (0, 15, math.inf):
                ax.annotate("τ=∞" if p["budget_s"] == math.inf else f"τ={p['budget_s']:g}s",
                            (p["cost_per_10k"], p["quality"]), textcoords="offset points", xytext=(4, -9), fontsize=6)
        ax.legend(fontsize=7)
    ax.set_xlabel("Estimated LLM cost ($ per 10k runs)")
    ax.set_ylabel("Overall feedback quality (%)")
    ax.set_title("Hybrid router cost–quality Pareto")
    return _save(fig, out, "fig6_hybrid_router_pareto_cost_vs_quality"), points


def _tex(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "--"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value).replace("_", r"\_").replace("&", r"\&").replace("%", r"\%")


def tables(stats: dict[str, dict[str, Any]], matrix: dict[str, list[float | None]], points: list[dict[str, Any]], out: Path) -> list[Path]:
    paths = []
    lines = [r"\begin{table}[t]", r"\centering\small", r"\caption{End-to-end QoS and feedback quality per model.}",
             r"\label{tab:qos}", r"\begin{tabular}{lrrrrrrrrrr}", r"\toprule",
             r"Model & $n$ & Succ.\,\% & Quality\,\% & P50\,s & P95\,s & 1st WS\,s & Tok/s & VRAM\,MB & \$/10k & SLA\,\% \\",
             r"\midrule"]
    for model, s in stats.items():
        lines.append(" & ".join([_tex(model), str(s["n"]), _tex(s["success_rate"]), _tex(s["quality"]),
                                 _tex(s["p50_s"], 2), _tex(s["p95_s"], 2), _tex(s["ttft_s"], 2),
                                 _tex(s["tokens_per_sec"]), _tex(s["peak_vram_mb"], 0), _tex(s["cost_per_10k"], 2),
                                 _tex(s["sla_percent"])]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    paths.append(out / "table1_qos_quality.tex")
    paths[-1].write_text("\n".join(lines) + "\n", encoding="utf-8")

    lines = [r"\begin{table}[t]", r"\centering\small", r"\caption{Quality (\%) by CodeNet status and diagnosis accuracy.}",
             r"\label{tab:status}", r"\begin{tabular}{l" + "r" * 10 + "}", r"\toprule",
             r" & \multicolumn{6}{c}{Quality score (\%)} & \multicolumn{4}{c}{Defect detected (\%)} \\",
             r"Model & CE & RE & WA & TLE/MLE & AC & All & CE & RE & WA & TLE/MLE \\", r"\midrule"]
    for model, s in stats.items():
        detect = matrix.get(model, [None] * 5)
        lines.append(" & ".join([_tex(model), *[_tex(s["by_status"].get(st)) for st in STATUSES], _tex(s["quality"]),
                                 *[_tex(v) for v in detect[:4]]]) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    paths.append(out / "table2_status_breakdown.tex")
    paths[-1].write_text("\n".join(lines) + "\n", encoding="utf-8")

    if points:
        lines = [r"\begin{table}[t]", r"\centering\small", r"\caption{Local-first hybrid router operating points.}",
                 r"\label{tab:hybrid}", r"\begin{tabular}{rrrr}", r"\toprule",
                 r"Budget $\tau$ (s) & Escalated (\%) & Quality (\%) & \$/10k \\", r"\midrule"]
        for p in points:
            budget = r"$\infty$" if p["budget_s"] == math.inf else f"{p['budget_s']:g}"
            lines.append(f"{budget} & {p['escalation_rate']:.1f} & {p['quality']:.1f} & {p['cost_per_10k']:.2f} \\\\")
        lines += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
        paths.append(out / "table3_hybrid_router.tex")
        paths[-1].write_text("\n".join(lines) + "\n", encoding="utf-8")
    return paths


def winner_markdown(stats: dict[str, dict[str, Any]]) -> str:
    best_local, _ = best_models(stats)
    lines = ["| Model | Provider | n | Success % | Quality % | P95 s | SLA % | $/10k | Peak VRAM MB |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for model, s in stats.items():
        name = f"**{model}** (winner)" if model == best_local else model
        cells = [s["success_rate"], s["quality"], s["p95_s"], s["sla_percent"], s["cost_per_10k"], s["peak_vram_mb"]]
        lines.append(f"| {name} | {s['provider']} | {s['n']} | " + " | ".join(
            "–" if c is None else f"{c:.1f}" if isinstance(c, float) else str(c) for c in cells) + " |")
    lines.append("")
    lines.append(f"Winning local model: **{best_local or 'n/a'}** (highest judged quality; ties broken by P95 latency).")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--judged", type=Path, default=LOG_DIR / "judged_logs.jsonl")
    parser.add_argument("--inference", type=Path, default=LOG_DIR / "inference_logs.jsonl")
    parser.add_argument("--burst", type=Path, default=LOG_DIR / "burst_logs.jsonl")
    parser.add_argument("--results", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    inference = latest(read_jsonl(args.inference))
    judged = {str(r["run_key"]): r for r in latest(read_jsonl(args.judged))}
    rows = [judged.get(str(r["run_key"]), r) for r in inference]
    if not rows:
        print("No benchmark records found.", file=sys.stderr)
        return 1
    burst = latest(read_jsonl(args.burst))
    graphs, tables_dir = args.results / "graphs", args.results / "tables"
    graphs.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)

    stats = summarize(rows)
    outputs = fig1(stats, graphs) + fig2(stats, graphs) + fig3(rows, stats, graphs)
    paths, matrix = fig4(rows, stats, graphs)
    outputs += paths + fig5(burst, stats, graphs)
    paths, points = fig6(rows, stats, graphs)
    outputs += paths + tables(stats, matrix, points, tables_dir)
    summary = winner_markdown(stats)
    (args.results / "summary.md").write_text(summary + "\n", encoding="utf-8")
    (args.results / "summary.json").write_text(json.dumps(
        {"models": stats, "diagnosis": matrix, "hybrid_router": points}, indent=2, default=str) + "\n", encoding="utf-8")
    print(summary)
    print("\nGenerated:")
    for path in outputs:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
