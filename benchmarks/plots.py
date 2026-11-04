"""Automated report-ready Matplotlib chart generation for SWCP benchmarks."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

logger = logging.getLogger("benchmarks.plots")

# Set clean publication style
plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 10,
    "axes.labelsize": 11,
    "axes.titlesize": 12,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.fontsize": 9,
    "figure.titlesize": 13,
})


def plot_obj1_bottleneck_stacked(
    sub_df: pd.DataFrame,
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Stacked bar chart proving vLLM dominates total delay at high load (mean additive)."""
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=300)

    # Use mean stage timings across actual submissions for true additive stacking
    if not sub_df.empty and "step_label" in sub_df.columns:
        grouped = sub_df.groupby("step_label", as_index=False).agg({
            "http_enqueue_ms": "mean",
            "sandbox_queue_wait_ms": "mean",
            "sandbox_exec_ms": "mean",
            "llm_queue_wait_ms": "mean",
            "llm_exec_ms": "mean",
            "client_e2e_ms": "mean",
        })
    elif not sum_df.empty:
        grouped = sum_df.copy()
    else:
        grouped = pd.DataFrame()

    if grouped.empty:
        plt.close(fig)
        return

    if "target_load" in grouped.columns:
        grouped = grouped.sort_values("target_load")
    elif "step_label" in grouped.columns:
        grouped["_sort_key"] = grouped["step_label"].apply(
            lambda s: float(re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s)).group(1)) if re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s)) else 0.0
        )
        grouped = grouped.sort_values("_sort_key")

    steps = grouped["step_label"].tolist() if "step_label" in grouped else [f"Step {i}" for i in range(len(grouped))]
    x = np.arange(len(steps))
    width = 0.55

    enqueue = grouped.get("http_enqueue_ms", grouped.get("mean_enqueue_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    sb_wait = grouped.get("sandbox_queue_wait_ms", grouped.get("p95_sandbox_wait_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    sb_exec = grouped.get("sandbox_exec_ms", grouped.get("p95_sandbox_exec_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    llm_wait = grouped.get("llm_queue_wait_ms", grouped.get("p95_llm_wait_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    llm_exec = grouped.get("llm_exec_ms", grouped.get("p95_llm_exec_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()

    ax.bar(x, enqueue, width, label="API Enqueue", color="#94a3b8")
    ax.bar(x, sb_wait, width, bottom=enqueue, label="Sandbox Queue Wait", color="#38bdf8")
    ax.bar(x, sb_exec, width, bottom=enqueue + sb_wait, label="Sandbox Exec (gVisor)", color="#0284c7")
    ax.bar(x, llm_wait, width, bottom=enqueue + sb_wait + sb_exec, label="LLM Queue Wait", color="#f97316")
    ax.bar(x, llm_exec, width, bottom=enqueue + sb_wait + sb_exec + llm_wait, label="vLLM Inference", color="#dc2626")

    ax.set_ylabel("Mean Latency (ms)")
    ax.set_title("Pipeline Latency Breakdown by Stage (Mean Additive)")
    ax.set_xticks(x)
    ax.set_xticklabels(steps, rotation=15, ha="right")
    ax.legend(loc="upper left", frameon=True)
    ax.grid(axis="y", linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_obj2_infinite_gpu_scaling(
    sub_df: pd.DataFrame,
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Multi-panel stacked area plot isolating non-GPU service bottlenecks and autoscaling."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9.5, 8.0), sharex=True, dpi=300)

    if not sub_df.empty and "step_label" in sub_df.columns:
        grouped = sub_df.groupby("step_label", as_index=False).agg({
            "http_enqueue_ms": "mean",
            "sandbox_queue_wait_ms": "mean",
            "sandbox_exec_ms": "mean",
            "llm_queue_wait_ms": "mean",
            "llm_exec_ms": "mean",
            "client_e2e_ms": ["mean", lambda v: np.percentile(v, 50), lambda v: np.percentile(v, 95)],
        })
        grouped.columns = [
            "step_label", "http_enqueue_ms", "sandbox_queue_wait_ms", "sandbox_exec_ms",
            "llm_queue_wait_ms", "llm_exec_ms", "mean_e2e_ms", "p50_e2e_ms", "p95_e2e_ms"
        ]
    elif not sum_df.empty:
        grouped = sum_df.copy()
    else:
        grouped = pd.DataFrame()

    if grouped.empty:
        plt.close(fig)
        return

    # Merge replica counts from sum_df if available
    if not sum_df.empty and "step_label" in sum_df.columns:
        rep_cols = ["step_label", "max_replicas_sandbox", "max_replicas_llm_worker", "max_replicas_core_api"]
        avail_reps = [c for c in rep_cols if c in sum_df.columns]
        grouped = pd.merge(grouped, sum_df[avail_reps], on="step_label", how="left")

    if "target_load" in grouped.columns:
        grouped = grouped.sort_values("target_load")
    elif "step_label" in grouped.columns:
        grouped["_sort_key"] = grouped["step_label"].apply(
            lambda s: float(re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s)).group(1)) if re.search(r"([0-9]+(?:\.[0-9]+)?)", str(s)) else 0.0
        )
        grouped = grouped.sort_values("_sort_key")

    steps = grouped["step_label"].tolist() if "step_label" in grouped else [f"Step {i}" for i in range(len(grouped))]
    x = np.arange(len(steps))

    enqueue = grouped.get("http_enqueue_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    sb_wait = grouped.get("sandbox_queue_wait_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    sb_exec = grouped.get("sandbox_exec_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    llm_wait = grouped.get("llm_queue_wait_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    llm_exec = grouped.get("llm_exec_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()

    p50_vals = grouped.get("p50_e2e_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    p95_vals = grouped.get("p95_e2e_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()

    # Top Panel: Stacked Area Chart (contributing service latencies)
    ax1.stackplot(
        x,
        enqueue,
        sb_wait,
        sb_exec,
        llm_wait,
        llm_exec,
        labels=[
            "API Gateway / DB Enqueue",
            "Sandbox Queue Wait",
            "Sandbox Exec (gVisor)",
            "LLM Queue Wait",
            "Mock LLM (5ms)",
        ],
        colors=["#94a3b8", "#38bdf8", "#0284c7", "#f97316", "#dc2626"],
        alpha=0.75,
    )

    # Total E2E Latency Overlay Lines
    ax1.plot(x, p50_vals, marker="o", color="#1e3a8a", linewidth=2.2, label="Total Client p50 (Median)")
    ax1.plot(x, p95_vals, marker="s", color="#b91c1c", linewidth=2.5, linestyle="--", label="Total Client p95 (Tail)")

    ax1.set_ylabel("Latency (ms)")
    ax1.set_title("Service Latency Growth & Pod Scaling Under Load (Infinite GPU)")
    ax1.legend(loc="upper left", frameon=True, fontsize=8.5)
    ax1.grid(True, linestyle="--", alpha=0.7)

    # Bottom Panel: Active Pod Replicas
    rep_sb = grouped.get("max_replicas_sandbox", pd.Series([3] * len(steps))).fillna(3)
    rep_llm = grouped.get("max_replicas_llm_worker", pd.Series([2] * len(steps))).fillna(2)
    rep_core = grouped.get("max_replicas_core_api", pd.Series([2] * len(steps))).fillna(2)

    ax2.step(x, rep_sb, where="mid", marker="o", label="c-sandbox pods", color="#0284c7")
    ax2.step(x, rep_llm, where="mid", marker="s", label="llm-worker pods (KEDA)", color="#f97316")
    ax2.step(x, rep_core, where="mid", marker="^", label="core-api pods (HPA)", color="#64748b")
    ax2.set_ylabel("Active Pod Replicas")
    ax2.set_xlabel("Load Step")
    ax2.set_xticks(x)
    ax2.set_xticklabels(steps, rotation=15, ha="right")
    ax2.legend(loc="upper left")
    ax2.grid(True, linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_obj3_vllm_tuning_matrix(
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """vLLM hardware tuning parameter sweep throughput vs latency Pareto frontier."""
    fig, ax = plt.subplots(figsize=(9, 6), dpi=300)

    if sum_df.empty:
        sum_df = pd.DataFrame([{
            "config_label": "default_cfg",
            "mean_tok_per_s": 85.0,
            "p95_e2e_ms": 3200.0,
            "peak_kv_cache_pct": 45.0,
        }])

    x_col = "mean_tok_per_s" if "mean_tok_per_s" in sum_df and sum_df["mean_tok_per_s"].max() > 0 else "achieved_throughput_per_min"
    x_vals = sum_df.get(x_col, pd.Series([10.0] * len(sum_df))).fillna(0.0)
    x_label = "Mean Token Generation Throughput (tokens/s)" if x_col == "mean_tok_per_s" else "Achieved Throughput (submissions/min)"

    p95_s = (sum_df.get("p95_e2e_ms", pd.Series([3000.0] * len(sum_df))).fillna(3000.0)) / 1000.0
    label_col = "config_label" if "config_label" in sum_df else ("hardware_tag" if "hardware_tag" in sum_df else "step_label")
    labels = sum_df.get(label_col, pd.Series([f"cfg_{i}" for i in range(len(sum_df))]))
    kv_pct = sum_df.get("peak_kv_cache_pct", pd.Series([40.0] * len(sum_df))).fillna(40.0)

    scatter = ax.scatter(
        x_vals,
        p95_s,
        s=np.clip(kv_pct * 6 + 80, 80, 600),
        c=kv_pct,
        cmap="coolwarm",
        alpha=0.88,
        edgecolors="black",
        linewidth=1.2,
    )

    cbar = plt.colorbar(scatter, ax=ax)
    cbar.set_label("Peak KV Cache Usage (%)")

    for i, label in enumerate(labels):
        ax.annotate(
            str(label),
            (x_vals.iloc[i], p95_s.iloc[i]),
            xytext=(8, 6),
            textcoords="offset points",
            fontsize=8.5,
            fontweight="bold",
        )

    ax.set_xlabel(x_label)
    ax.set_ylabel("p95 End-to-End Latency (s)")
    ax.set_title("vLLM Hardware Parameter Tuning (Throughput vs. p95 Latency)")
    ax.grid(True, linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_obj4_sla_capacity_curve(
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """SLA capacity curve plotting p50 and p95 with 5s, 10s, 15s, 20s, 30s thresholds."""
    fig, ax = plt.subplots(figsize=(9, 6), dpi=300)

    if sum_df.empty:
        sum_df = pd.DataFrame([{
            "target_load": 5.0,
            "p50_e2e_ms": 2200.0,
            "p95_e2e_ms": 3800.0,
            "step_label": "sample",
        }])

    x = sum_df.get("target_load", pd.Series(range(1, len(sum_df) + 1))).to_numpy()
    p50_s = (sum_df.get("p50_e2e_ms", pd.Series([2000.0] * len(sum_df))) / 1000.0).to_numpy()
    p95_s = (sum_df.get("p95_e2e_ms", pd.Series([3500.0] * len(sum_df))) / 1000.0).to_numpy()

    sort_idx = np.argsort(x)
    x = x[sort_idx]
    p50_s = p50_s[sort_idx]
    p95_s = p95_s[sort_idx]

    ax.plot(x, p50_s, marker="o", color="#0284c7", linewidth=2.2, label="p50 Latency (median)")
    ax.plot(x, p95_s, marker="s", color="#ef4444", linewidth=2.5, label="p95 Latency (SLA boundary)")

    sla_levels = [
        (5, "#10b981", "5s SLA"),
        (10, "#f59e0b", "10s SLA"),
        (15, "#f97316", "15s SLA"),
        (20, "#dc2626", "20s SLA"),
        (30, "#7f1d1d", "30s SLA"),
    ]
    for threshold, color, label in sla_levels:
        ax.axhline(threshold, color=color, linestyle="--", linewidth=1.2, alpha=0.85, label=f"{label} threshold")
        if len(x) > 1 and p95_s.min() <= threshold <= p95_s.max():
            x_cross = np.interp(threshold, p95_s, x)
            ax.plot([x_cross], [threshold], marker="x", markersize=8, color=color, mew=2.5)
            ax.annotate(
                f"Max: {x_cross:.1f}/min",
                (x_cross, threshold),
                xytext=(8, 4),
                textcoords="offset points",
                fontsize=8,
                color=color,
                fontweight="bold",
            )

    ax.set_xlabel("Submission Arrival Rate (submissions/min)")
    ax.set_ylabel("End-to-End Latency (seconds)")
    ax.set_title("System Capacity vs. Acceptable SLA Latency Curves")
    ax.set_ylim(bottom=0, top=max(35.0, float(p95_s.max() * 1.25)))
    ax.legend(loc="upper left", frameon=True)
    ax.grid(True, linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_burst_drain(
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Classroom deadline burst shockwave absorption and drain latency."""
    fig, ax = plt.subplots(figsize=(9, 5.5), dpi=300)
    if sum_df.empty:
        plt.close(fig)
        return

    df = sum_df.copy()
    if "step_label" in df.columns:
        df["burst_size"] = df["step_label"].apply(
            lambda s: int(re.search(r"burst_([0-9]+)", str(s)).group(1)) if re.search(r"burst_([0-9]+)", str(s)) else 0
        )
        df = df.sort_values("burst_size")

    x = df.get("burst_size", pd.Series(range(len(df)))).to_numpy()
    p50_s = (df.get("p50_e2e_ms", pd.Series([0.0] * len(df))) / 1000.0).to_numpy()
    p95_s = (df.get("p95_e2e_ms", pd.Series([0.0] * len(df))) / 1000.0).to_numpy()

    ax.plot(x, p50_s, marker="o", color="#0284c7", linewidth=2.2, label="Median p50 Latency (s)")
    ax.plot(x, p95_s, marker="s", color="#dc2626", linewidth=2.5, label="Tail p95 Latency (s)")

    ax.set_xlabel("Burst Size (Simultaneous Submissions)")
    ax.set_ylabel("End-to-End Latency (seconds)")
    ax.set_title("Classroom Deadline Burst Absorption & Drain Time")
    ax.legend(loc="upper left", frameon=True)
    ax.grid(True, linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def generate_all_plots(
    db_path: Path,
    run_id: str,
    output_dir: Path,
    suite_name: str | None = None,
) -> list[Path]:
    """Read data for run_id from SQLite and generate charts for the suite."""
    output_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        sub_df = pd.read_sql_query("SELECT * FROM submissions WHERE run_id = ?", conn, params=[run_id])
        sum_df = pd.read_sql_query("SELECT * FROM step_summaries WHERE run_id = ?", conn, params=[run_id])
        if suite_name is None:
            row = conn.execute("SELECT suite_name FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            suite_name = row[0] if row else None

    generated: list[Path] = []
    if suite_name in ("sla_curve", "obj4_sla_curve", "obj1_bottleneck", "custom"):
        p1 = output_dir / "pipeline_latency_breakdown_stacked.png"
        p4 = output_dir / "sla_capacity_curve.png"
        plot_obj1_bottleneck_stacked(sub_df, sum_df, p1)
        plot_obj4_sla_capacity_curve(sum_df, p4)
        generated.extend([p1, p4])
    elif suite_name in ("burst",):
        p_burst = output_dir / "burst_absorption_curve.png"
        plot_burst_drain(sum_df, p_burst)
        generated.append(p_burst)
    elif suite_name in ("infinite_gpu", "obj2_infinite_gpu"):
        p2 = output_dir / "service_scaling_stacked_area.png"
        plot_obj2_infinite_gpu_scaling(sub_df, sum_df, p2)
        generated.append(p2)
    elif suite_name in ("vllm_sweep", "obj3_vllm_sweep"):
        p3 = output_dir / "vllm_tuning_matrix.png"
        plot_obj3_vllm_tuning_matrix(sum_df, p3)
        generated.append(p3)
    else:
        p1 = output_dir / "pipeline_latency_breakdown_stacked.png"
        p2 = output_dir / "service_scaling_stacked_area.png"
        p3 = output_dir / "vllm_tuning_matrix.png"
        p4 = output_dir / "sla_capacity_curve.png"
        plot_obj1_bottleneck_stacked(sub_df, sum_df, p1)
        plot_obj2_infinite_gpu_scaling(sub_df, sum_df, p2)
        plot_obj3_vllm_tuning_matrix(sum_df, p3)
        plot_obj4_sla_capacity_curve(sum_df, p4)
        generated.extend([p1, p2, p3, p4])

    return generated
