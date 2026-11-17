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


def _place_legend_above(
    ax: plt.Axes,
    handles: list[Any] | None = None,
    labels: list[str] | None = None,
    ncol: int = 3,
    fontsize: float = 8.5,
    title: str | None = None,
    title_fontsize: float = 11.5,
) -> None:
    """Place legend in its own framed box above the plotting area, and position title above it."""
    if handles is None or labels is None:
        handles, labels = ax.get_legend_handles_labels()
    if handles:
        n_rows = max(1, (len(handles) + ncol - 1) // ncol)
        ax.legend(
            handles,
            labels,
            loc="lower left",
            bbox_to_anchor=(0.0, 1.02),
            ncol=ncol,
            frameon=True,
            fontsize=fontsize,
            borderaxespad=0.0,
        )
        title_pad = 12 + n_rows * 16
    else:
        title_pad = 8
    if title:
        ax.set_title(title, pad=title_pad, fontsize=title_fontsize)


def _sort_step_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "target_load" in out.columns and pd.to_numeric(out["target_load"], errors="coerce").notna().any():
        out["_sort_key"] = pd.to_numeric(out["target_load"], errors="coerce").fillna(0.0)
        return out.sort_values("_sort_key").drop(columns=["_sort_key"])
    if "step_label" in out.columns:
        out["_sort_key"] = out["step_label"].apply(
            lambda s: float(re.findall(r"([0-9]+(?:\.[0-9]+)?)", str(s))[-1])
            if re.findall(r"([0-9]+(?:\.[0-9]+)?)", str(s))
            else 0.0
        )
        return out.sort_values("_sort_key").drop(columns=["_sort_key"])
    return out


def _build_step_summary_frame(sub_df: pd.DataFrame, sum_df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate per-submission stage means merged with step summary telemetry."""
    if not sub_df.empty and "step_label" in sub_df.columns:
        has_telem = (sub_df.get("sandbox_exec_ms", 0) > 0) | (sub_df.get("llm_exec_ms", 0) > 0)
        stage_df = sub_df[has_telem].copy() if has_telem.any() else sub_df.copy()
        if "final_status" in stage_df.columns and "delivery_overhead_ms" in stage_df.columns:
            # Exclude 360s client timeout sentinel from delivery overhead mean
            stage_df.loc[stage_df["final_status"] == "TIMEOUT", "delivery_overhead_ms"] = np.nan

        agg_spec: dict[str, Any] = {
            "http_enqueue_ms": "mean",
            "sandbox_queue_wait_ms": "mean",
            "sandbox_exec_ms": "mean",
            "llm_queue_wait_ms": "mean",
            "llm_exec_ms": "mean",
            "client_e2e_ms": "mean",
        }
        for opt_col in ("orchestration_overhead_ms", "submit_edge_network_ms", "delivery_overhead_ms"):
            if opt_col in stage_df.columns:
                agg_spec[opt_col] = "mean"
        grouped = stage_df.groupby("step_label", as_index=False).agg(agg_spec)
        if not sum_df.empty and "step_label" in sum_df.columns:
            extra_cols = [c for c in sum_df.columns if c not in grouped.columns and c != "run_id"]
            if extra_cols:
                grouped = pd.merge(grouped, sum_df[["step_label"] + extra_cols], on="step_label", how="left")
    elif not sum_df.empty:
        grouped = sum_df.copy()
    else:
        grouped = pd.DataFrame()
    return _sort_step_df(grouped)


def plot_obj1_bottleneck_stacked(
    sub_df: pd.DataFrame,
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Stacked bar chart proving vLLM dominates total delay at high load (mean additive)."""
    fig, ax = plt.subplots(figsize=(9.5, 5.5), dpi=300)

    grouped = _build_step_summary_frame(sub_df, sum_df)
    if grouped.empty:
        plt.close(fig)
        return

    raw_steps = grouped["step_label"].tolist() if "step_label" in grouped else [f"Step {i}" for i in range(len(grouped))]
    is_ramp = bool(raw_steps) and all(str(s).startswith("ramp_") for s in raw_steps) and "target_load" in grouped.columns
    if is_ramp:
        steps = [f"{int(round(float(v or 0)))}" for v in grouped["target_load"].tolist()]
    else:
        steps = []
        for idx, raw_lbl in enumerate(raw_steps):
            lbl = str(raw_lbl)
            if "is_sustained" in grouped.columns and int(grouped["is_sustained"].iloc[idx] or 0) == 0:
                dur = float(grouped["step_duration_actual_s"].iloc[idx] or 0.0) if "step_duration_actual_s" in grouped.columns else 0.0
                lbl = f"{lbl}\n[Overload @{dur:.0f}s]" if dur > 0 else f"{lbl}\n[Overload]"
            steps.append(lbl)
    x = np.arange(len(steps))
    width = 0.55

    enqueue = grouped.get("http_enqueue_ms", grouped.get("mean_enqueue_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    sb_wait = grouped.get("sandbox_queue_wait_ms", grouped.get("p95_sandbox_wait_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    sb_exec = grouped.get("sandbox_exec_ms", grouped.get("p95_sandbox_exec_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    llm_wait = grouped.get("llm_queue_wait_ms", grouped.get("p95_llm_wait_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    llm_exec = grouped.get("llm_exec_ms", grouped.get("p95_llm_exec_ms", pd.Series([0.0] * len(steps)))).fillna(0).to_numpy()
    orch = grouped.get("orchestration_overhead_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    edge_delivery = (
        grouped.get("submit_edge_network_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
        + grouped.get("delivery_overhead_ms", pd.Series([0.0] * len(steps))).fillna(0).to_numpy()
    )

    ax.bar(x, enqueue, width, label="API Enqueue", color="#94a3b8")
    ax.bar(x, sb_wait, width, bottom=enqueue, label="Sandbox Queue Wait", color="#38bdf8")
    ax.bar(x, sb_exec, width, bottom=enqueue + sb_wait, label="Sandbox Exec (gVisor)", color="#0284c7")
    ax.bar(x, llm_wait, width, bottom=enqueue + sb_wait + sb_exec, label="LLM Queue Wait", color="#f97316")
    ax.bar(x, llm_exec, width, bottom=enqueue + sb_wait + sb_exec + llm_wait, label="vLLM Inference", color="#dc2626")
    top = enqueue + sb_wait + sb_exec + llm_wait + llm_exec
    if np.any(orch > 0):
        ax.bar(x, orch, width, bottom=top, label="Bus / Orchestration", color="#a855f7")
        top = top + orch
    if np.any(edge_delivery > 0):
        ax.bar(x, edge_delivery, width, bottom=top, label="Edge Network + Delivery", color="#475569")

    ax.set_ylabel("Mean Latency (ms)")
    ax.set_xticks(x)
    if is_ramp:
        ax.set_xlabel("Submission Rate (subs/min)")
        ax.set_xticklabels(steps, rotation=0, ha="center")
        legend_title = "Pipeline Latency Breakdown by Stage (Per-Bucket Mean Additive)"
    else:
        ax.set_xticklabels(steps, rotation=15, ha="right")
        legend_title = "Pipeline Latency Breakdown by Stage (Steady Regimes vs. Windowed Overload)"
    _place_legend_above(
        ax, ncol=4, fontsize=8.5,
        title=legend_title,
    )
    ax.grid(axis="y", linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_obj2_infinite_gpu_scaling(
    sub_df: pd.DataFrame,
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Multi-panel chart isolating non-GPU throughput ceiling (sandbox node split), pod scaling, and bucket latency."""
    fig, (ax0, ax1, ax2) = plt.subplots(3, 1, figsize=(10.5, 13.8), sharex=False, dpi=300)

    grouped = _build_step_summary_frame(sub_df, sum_df)
    if grouped.empty:
        plt.close(fig)
        return

    if not sub_df.empty and "step_label" in sub_df.columns:
        pct = sub_df.groupby("step_label")["client_e2e_ms"].agg(
            p50=lambda v: float(np.percentile(v, 50)),
            p95=lambda v: float(np.percentile(v, 95)),
        ).reset_index()
        grouped = pd.merge(grouped, pct, on="step_label", how="left")
        grouped["p50_e2e_ms"] = grouped["p50"].fillna(grouped.get("p50_e2e_ms", 0.0))
        grouped["p95_e2e_ms"] = grouped["p95"].fillna(grouped.get("p95_e2e_ms", 0.0))

        if "sandbox_node" in sub_df.columns:
            node_counts = (
                sub_df.assign(
                    _is_worker=sub_df["sandbox_node"].fillna("").astype(str).apply(
                        lambda n: 1 if (n != "" and n != "control-node") else 0
                    ),
                    _is_ctrl=sub_df["sandbox_node"].fillna("").astype(str).apply(
                        lambda n: 1 if n == "control-node" else 0
                    ),
                )
                .groupby("step_label", as_index=False)
                .agg(sb_ctrl_reqs=("_is_ctrl", "sum"), sb_worker_reqs=("_is_worker", "sum"))
            )
            grouped = pd.merge(grouped, node_counts, on="step_label", how="left")

    grouped = _sort_step_df(grouped)
    target_rpm = grouped.get("target_load", pd.Series(range(1, len(grouped) + 1))).fillna(0).to_numpy(dtype=float)
    labels = [f"{int(round(v))}" for v in target_rpm]
    x = np.arange(len(labels))

    achieved_rpm = grouped.get("achieved_throughput_per_min", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    dur_min = np.maximum(0.05, grouped.get("step_duration_actual_s", pd.Series([30.0] * len(labels))).fillna(30.0).to_numpy(dtype=float) / 60.0)

    sb_ctrl_raw = grouped.get("sb_ctrl_reqs", pd.Series([1.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    sb_worker_raw = grouped.get("sb_worker_reqs", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    tot_sb_reqs = sb_ctrl_raw + sb_worker_raw
    ctrl_share = np.where(tot_sb_reqs > 0, sb_ctrl_raw / np.maximum(1.0, tot_sb_reqs), 1.0)
    worker_share = np.where(tot_sb_reqs > 0, sb_worker_raw / np.maximum(1.0, tot_sb_reqs), 0.0)
    comp_ctrl_rpm = achieved_rpm * ctrl_share
    comp_worker_rpm = achieved_rpm * worker_share

    if "backlog_growth_per_min" in grouped.columns and grouped["backlog_growth_per_min"].notna().any():
        backlog_growth_rpm = grouped["backlog_growth_per_min"].fillna(0).to_numpy(dtype=float)
    else:
        backlog_end = grouped.get("in_flight_end", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
        prev_backlog = np.concatenate(([0.0], backlog_end[:-1]))
        backlog_growth_rpm = (backlog_end - prev_backlog) / dur_min
    backlog_pos_rpm = np.maximum(0.0, backlog_growth_rpm)

    # Top Panel (Single Y-axis: subs/min): Target 1:1 vs Sandbox-Node-Split Completion Rate + Queue Growth (+dQ/dt)
    ax0.bar(
        x, comp_ctrl_rpm, width=0.56, color="#3b82f6", alpha=0.88,
        label="Completed: Control-Node Sandbox (subs/min)",
    )
    ax0.bar(
        x, comp_worker_rpm, width=0.56, bottom=comp_ctrl_rpm, color="#f97316", alpha=0.90,
        label="Completed: Worker-Node Sandbox (subs/min)",
    )
    ax0.bar(
        x, backlog_pos_rpm, width=0.56, bottom=achieved_rpm,
        color="#fde68a", edgecolor="#d97706", hatch="///", alpha=0.72, linewidth=0.9,
        label="Backlog Growth +dQ/dt (subs/min)",
    )
    ax0.plot(
        x, target_rpm, linestyle="--", color="#475569", linewidth=1.9,
        label="Target Arrival Rate 1:1 (subs/min)",
    )
    ax0.plot(
        x, achieved_rpm, color="#0f172a", linewidth=2.2, marker="o", markersize=5.5,
        label="Total Completion Rate (subs/min)",
    )
    ax0.set_ylabel("Throughput / Queue Flow (subs/min)")
    ax0.set_xlabel("Submission Rate (subs/min)")
    ax0.set_xticks(x)
    ax0.set_xticklabels(labels)
    ax0.set_ylim(bottom=0)
    ax0.grid(True, linestyle="--", alpha=0.7)
    _place_legend_above(
        ax0, ncol=3, fontsize=8.3,
        title="Infinite-GPU Scaling: Non-GPU Ceiling & Sandbox Node Spillover",
    )

    # Middle Panel: Active Pod Replicas & Worker Node Spillover
    rep_sb = grouped.get("max_replicas_sandbox", pd.Series([3] * len(labels))).fillna(3)
    rep_llm = grouped.get("max_replicas_llm_worker", pd.Series([2] * len(labels))).fillna(2)
    rep_core = grouped.get("max_replicas_core_api", pd.Series([2] * len(labels))).fillna(2)

    ax1.step(x, rep_sb, where="mid", marker="o", linewidth=2.0, label="c-sandbox pods (total)", color="#0284c7")
    ax1.step(x, rep_llm, where="mid", marker="s", linewidth=1.8, label="llm-worker pods (total)", color="#f97316")
    ax1.step(x, rep_core, where="mid", marker="^", linewidth=1.8, label="core-api pods (total)", color="#64748b")

    rep_sb_w = grouped.get("max_replicas_sandbox_worker", pd.Series([0] * len(labels))).fillna(0)
    rep_llm_w = grouped.get("max_replicas_llm_worker_worker", pd.Series([0] * len(labels))).fillna(0)
    if float(rep_sb_w.max()) > 0:
        ax1.step(x, rep_sb_w, where="mid", linestyle="--", marker="o", linewidth=1.8, label="c-sandbox pods (worker-node)", color="#38bdf8")
    if float(rep_llm_w.max()) > 0:
        ax1.step(x, rep_llm_w, where="mid", linestyle="--", marker="s", linewidth=1.8, label="llm-worker pods (worker-node)", color="#fb923c")
    ax1.set_ylabel("Active Pod Replicas")
    ax1.set_xlabel("Submission Rate (subs/min)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.grid(True, linestyle="--", alpha=0.7)
    _place_legend_above(
        ax1, ncol=3, fontsize=8.5,
        title="Cluster Autoscaling (KEDA / HPA Active Pod Replicas & Worker Spillover)",
        title_fontsize=10.5,
    )

    # Bottom Panel: Rolling Bucket Latency (s) + Failure Annotations
    p50_s = grouped.get("p50_e2e_ms", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float) / 1000.0
    p95_s = grouped.get("p95_e2e_ms", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float) / 1000.0
    succ = grouped.get("success_rate", pd.Series([1.0] * len(labels))).fillna(1.0).to_numpy(dtype=float) * 100.0

    ax2.plot(x, p50_s, marker="o", color="#0284c7", linewidth=2.0, label="Bucket p50 E2E (s)")
    ax2.plot(x, p95_s, marker="s", color="#dc2626", linewidth=2.3, label="Bucket p95 E2E (s)")

    fail_mask = succ < 99.95
    if np.any(fail_mask):
        ax2.scatter(
            x[fail_mask], p95_s[fail_mask], s=110, facecolors="none", edgecolors="#991b1b",
            linewidths=2.2, zorder=5, label="Bucket With Failures (<100% OK)",
        )
        for xi, yi, sr in zip(x[fail_mask], p95_s[fail_mask], succ[fail_mask]):
            ax2.annotate(
                f"{sr:.0f}% OK", (xi, yi), xytext=(0, 8), textcoords="offset points",
                ha="center", fontsize=8, fontweight="bold", color="#991b1b",
            )

    ax2.set_ylabel("Latency (seconds)")
    ax2.set_xlabel("Submission Rate (subs/min)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels)
    ax2.set_ylim(bottom=0, top=max(1.0, float(np.max(p95_s)) * 1.18))
    ax2.grid(True, linestyle="--", alpha=0.7)
    fail_suffix = "" if np.any(fail_mask) else " — 100% Success All Buckets"
    _place_legend_above(
        ax2, ncol=3, fontsize=8.5,
        title=f"Rolling Bucket Latency (Transient Ramp — Includes Prior Backlog{fail_suffix})",
        title_fontsize=10.5,
    )

    plt.tight_layout(h_pad=3.0)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_ramp_spillover_capacity(
    sub_df: pd.DataFrame,
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """2-node continuous-ramp capacity chart showing throughput, GPU split, backlog growth, health diagnostics, and latency."""
    fig, (ax0, ax1, ax2) = plt.subplots(3, 1, figsize=(10.5, 13.8), sharex=False, dpi=300)

    grouped = _build_step_summary_frame(sub_df, sum_df)
    if grouped.empty:
        plt.close(fig)
        return

    target_rpm = grouped.get("target_load", pd.Series(range(1, len(grouped) + 1))).fillna(0).to_numpy(dtype=float)
    labels = [f"{int(round(v))}" for v in target_rpm]
    x = np.arange(len(labels))

    achieved_rpm = grouped.get("achieved_throughput_per_min", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    dur_min = np.maximum(0.05, grouped.get("step_duration_actual_s", pd.Series([30.0] * len(labels))).fillna(30.0).to_numpy(dtype=float) / 60.0)
    prim_raw = grouped.get("primary_requests", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    ovf_raw = grouped.get("overflow_requests", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)

    # Partition Achieved Completion Rate by each GPU's observed request share
    tot_gpu_reqs = prim_raw + ovf_raw
    prim_share = np.where(tot_gpu_reqs > 0, prim_raw / np.maximum(1.0, tot_gpu_reqs), 1.0)
    ovf_share = np.where(tot_gpu_reqs > 0, ovf_raw / np.maximum(1.0, tot_gpu_reqs), 0.0)
    comp_prim_rpm = achieved_rpm * prim_share
    comp_ovf_rpm = achieved_rpm * ovf_share

    if "backlog_growth_per_min" in grouped.columns and grouped["backlog_growth_per_min"].notna().any():
        backlog_growth_rpm = grouped["backlog_growth_per_min"].fillna(0).to_numpy(dtype=float)
    else:
        backlog_end = grouped.get("in_flight_end", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
        prev_backlog = np.concatenate(([0.0], backlog_end[:-1]))
        backlog_growth_rpm = (backlog_end - prev_backlog) / dur_min
    backlog_pos_rpm = np.maximum(0.0, backlog_growth_rpm)

    # Top Panel (Single Y-axis: subs/min): Target 1:1 vs GPU-Split Completion Rate + Queue Growth (+dQ/dt)
    ax0.bar(
        x, comp_prim_rpm, width=0.56, color="#3b82f6", alpha=0.88,
        label="Completed: Primary GPU (subs/min)",
    )
    ax0.bar(
        x, comp_ovf_rpm, width=0.56, bottom=comp_prim_rpm, color="#f97316", alpha=0.90,
        label="Completed: Overflow GPU (subs/min)",
    )
    ax0.bar(
        x, backlog_pos_rpm, width=0.56, bottom=achieved_rpm,
        color="#fde68a", edgecolor="#d97706", hatch="///", alpha=0.72, linewidth=0.9,
        label="Backlog Growth +dQ/dt (subs/min)",
    )
    ax0.plot(
        x, target_rpm, linestyle="--", color="#475569", linewidth=1.9,
        label="Target Arrival Rate 1:1 (subs/min)",
    )
    ax0.plot(
        x, achieved_rpm, color="#0f172a", linewidth=2.2, marker="o", markersize=5.5,
        label="Total Completion Rate (subs/min)",
    )
    ax0.set_ylabel("Throughput / Queue Flow (subs/min)")
    ax0.set_xlabel("Submission Rate (subs/min)")
    ax0.set_xticks(x)
    ax0.set_xticklabels(labels)
    ax0.set_ylim(bottom=0)
    ax0.grid(True, linestyle="--", alpha=0.7)
    _place_legend_above(
        ax0, ncol=3, fontsize=8.3,
        title="2-Node Spillover & Dual-GPU Continuous Ramp Capacity",
    )

    # Middle Panel: Diagnostic Cluster Health (GPU Dispatches + KV Cache % + Worker Spilled Pods)
    tot_reqs = grouped.get("total_requests", pd.Series(tot_gpu_reqs)).fillna(0).to_numpy(dtype=float)
    eff_reqs = np.maximum(tot_reqs, tot_gpu_reqs)
    prim_rpm = (eff_reqs * prim_share) / dur_min
    ovf_rpm = (eff_reqs * ovf_share) / dur_min
    ax1.bar(x, prim_rpm, width=0.55, color="#3b82f6", alpha=0.85, label="Primary GPU (control-node req/min)")
    ax1.bar(x, ovf_rpm, width=0.55, bottom=prim_rpm, color="#f97316", alpha=0.90, label="Overflow GPU (worker-node req/min)")
    ax1.set_ylabel("LLM Dispatches / min")
    ax1.set_xlabel("Submission Rate (subs/min)")
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.grid(True, linestyle="--", alpha=0.7)

    ax1_r = ax1.twinx()
    prim_kv = grouped.get("primary_kv_cache_pct", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    ovf_kv = grouped.get("overflow_kv_cache_pct", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float)
    worker_pods = (
        grouped.get("max_replicas_sandbox_worker", pd.Series([0] * len(labels))).fillna(0).to_numpy(dtype=float)
        + grouped.get("max_replicas_llm_worker_worker", pd.Series([0] * len(labels))).fillna(0).to_numpy(dtype=float)
    )
    ax1_r.plot(x, prim_kv, color="#1d4ed8", linestyle="--", linewidth=1.6, label="Primary KV Cache (%)")
    ax1_r.plot(x, ovf_kv, color="#c2410c", linestyle="--", linewidth=1.6, label="Overflow KV Cache (%)")
    ax1_r.step(x, worker_pods * 10.0, where="mid", color="#7c3aed", linewidth=1.8, label="Worker Spilled Pods (x10)")
    ax1_r.set_ylabel("KV Cache (%) / Scaled Pods")
    m1, ml1 = ax1.get_legend_handles_labels()
    m2, ml2 = ax1_r.get_legend_handles_labels()
    _place_legend_above(
        ax1, m1 + m2, ml1 + ml2, ncol=3, fontsize=8.0,
        title="Diagnostic Cluster Health (GPU Dispatches, KV Cache Pressure & Worker Spilled Pods)",
        title_fontsize=10.5,
    )

    # Bottom Panel (Single Y-axis: Latency (s), surfacing failures inline only if < 100%)
    p50_s = grouped.get("p50_e2e_ms", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float) / 1000.0
    p95_s = grouped.get("p95_e2e_ms", pd.Series([0.0] * len(labels))).fillna(0).to_numpy(dtype=float) / 1000.0
    succ = grouped.get("success_rate", pd.Series([1.0] * len(labels))).fillna(1.0).to_numpy(dtype=float) * 100.0

    ax2.plot(x, p50_s, marker="o", color="#0284c7", linewidth=2.0, label="Bucket p50 E2E (s)")
    ax2.plot(x, p95_s, marker="s", color="#dc2626", linewidth=2.3, label="Bucket p95 E2E (s)")

    fail_mask = succ < 99.95
    if np.any(fail_mask):
        ax2.scatter(
            x[fail_mask], p95_s[fail_mask], s=110, facecolors="none", edgecolors="#991b1b",
            linewidths=2.2, zorder=5, label="Bucket With Failures (<100% OK)",
        )
        for xi, yi, sr in zip(x[fail_mask], p95_s[fail_mask], succ[fail_mask]):
            ax2.annotate(
                f"{sr:.0f}% OK", (xi, yi), xytext=(0, 8), textcoords="offset points",
                ha="center", fontsize=8, fontweight="bold", color="#991b1b",
            )

    ax2.set_ylabel("Latency (seconds)")
    ax2.set_xlabel("Submission Rate (subs/min)")
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels)
    ax2.set_ylim(bottom=0, top=max(1.0, float(np.max(p95_s)) * 1.18))
    ax2.grid(True, linestyle="--", alpha=0.7)
    fail_suffix = "" if np.any(fail_mask) else " — 100% Success All Buckets"
    _place_legend_above(
        ax2, ncol=3, fontsize=8.5,
        title=f"Rolling Bucket Latency (Transient Ramp — Includes Prior Backlog{fail_suffix})",
        title_fontsize=10.5,
    )

    plt.tight_layout(h_pad=3.0)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.2)
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

    # Add subtle right/top padding so labels near the rightmost/topmost points fit cleanly
    x_min, x_max = float(x_vals.min()), float(x_vals.max())
    y_min, y_max = float(p95_s.min()), float(p95_s.max())
    x_span = max(x_max - x_min, 10.0)
    y_span = max(y_max - y_min, 1.0)
    ax.set_xlim(x_min - 0.08 * x_span, x_max + 0.24 * x_span)
    ax.set_ylim(max(0.0, y_min - 0.12 * y_span), y_max + 0.14 * y_span)

    # Sort by x then y and alternate/nudge offsets when labels sit close together
    pts = [
        (float(x_vals.iloc[i]), float(p95_s.iloc[i]), str(labels.iloc[i]))
        for i in range(len(labels))
    ]
    pts.sort(key=lambda t: (t[0], t[1]))
    placed: list[tuple[float, float]] = []
    candidate_offsets = [
        (8, 7),
        (8, -13),
        (-12, 13),
        (8, 17),
        (-12, -17),
        (10, -24),
        (10, 26),
    ]
    for px, py, text_label in pts:
        best_off = candidate_offsets[0]
        for dx_pt, dy_pt in candidate_offsets:
            cand_x = px + (dx_pt / 72.0) * (x_span / 6.5)
            cand_y = py + (dy_pt / 72.0) * (y_span / 4.2)
            collision = any(
                abs(cand_x - ox) < 0.22 * x_span and abs(cand_y - oy) < 0.075 * y_span
                for ox, oy in placed
            )
            if not collision:
                best_off = (dx_pt, dy_pt)
                placed.append((cand_x, cand_y))
                break
        else:
            cand_x = px + (best_off[0] / 72.0) * (x_span / 6.5)
            cand_y = py + (best_off[1] / 72.0) * (y_span / 4.2)
            placed.append((cand_x, cand_y))

        ax.annotate(
            text_label,
            (px, py),
            xytext=best_off,
            textcoords="offset points",
            fontsize=8.5,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.72),
            arrowprops=dict(arrowstyle="-", color="#94a3b8", lw=0.7, alpha=0.8) if abs(best_off[1]) > 14 else None,
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
    """Honest SLA capacity curve separating Sustained Capacity from Windowed T-second Burst SLAs."""
    fig, ax = plt.subplots(figsize=(9.5, 6), dpi=300)

    if sum_df.empty:
        sum_df = pd.DataFrame([{
            "target_load": 5.0,
            "p50_e2e_ms": 2200.0,
            "p95_e2e_ms": 3800.0,
            "step_duration_actual_s": 90.0,
            "is_sustained": 1,
            "step_label": "sample",
        }])

    df = _sort_step_df(sum_df)
    x = df.get("target_load", pd.Series(range(1, len(df) + 1))).to_numpy(dtype=float)
    p50_s = (df.get("p50_e2e_ms", pd.Series([2000.0] * len(df))).fillna(0) / 1000.0).to_numpy(dtype=float)
    p95_s = (df.get("p95_e2e_ms", pd.Series([3500.0] * len(df))).fillna(0) / 1000.0).to_numpy(dtype=float)
    sustained = df.get("is_sustained", pd.Series([1] * len(df))).fillna(1).to_numpy(dtype=int)
    durs = df.get("step_duration_actual_s", pd.Series([90.0] * len(df))).fillna(90.0).to_numpy(dtype=float)
    win_s = float(np.median(durs)) if len(durs) > 0 else 90.0

    sustained_mask = sustained == 1
    sustained_max = float(x[sustained_mask].max()) if np.any(sustained_mask) else float(x.min())

    if len(x) > 1 and np.any(~sustained_mask):
        ax.axvspan(float(x.min()), sustained_max, color="#dcfce7", alpha=0.40, label="Sustained Steady Zone (dQ/dt ~ 0)")
        ax.axvspan(sustained_max, float(x.max()), color="#fef3c7", alpha=0.38, label=f"Finite {win_s:.0f}s Burst Overload Zone")
        ax.axvline(sustained_max, color="#15803d", linestyle="-.", linewidth=1.6, label=f"Sustained Knee (~{sustained_max:.0f}/min)")

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
            x_cross = float(np.interp(threshold, p95_s, x))
            prefix = "Sustained" if x_cross <= sustained_max or not np.any(~sustained_mask) else f"{win_s:.0f}s Burst"
            ax.plot([x_cross], [threshold], marker="x", markersize=8, color=color, mew=2.5)
            ax.annotate(
                f"{prefix}: {x_cross:.1f}/min",
                (x_cross, threshold),
                xytext=(8, 4),
                textcoords="offset points",
                fontsize=8,
                color=color,
                fontweight="bold",
            )

    ax.set_xlabel("Submission Arrival Rate (submissions/min)")
    ax.set_ylabel("End-to-End Latency (seconds)")
    ax.set_ylim(bottom=0, top=max(35.0, float(p95_s.max() * 1.25)))
    _place_legend_above(
        ax, ncol=3, fontsize=8.0,
        title=f"System Capacity vs. SLA Latency (Steady Knee vs. {win_s:.0f}s Burst Overload)",
    )
    ax.grid(True, linestyle="--", alpha=0.7)

    plt.tight_layout()
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    logger.info("Saved %s", output_path)


def plot_burst_drain(
    sum_df: pd.DataFrame,
    output_path: Path,
) -> None:
    """Classroom deadline burst shockwave absorption, total drain time, spillover %, and success rate."""
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(9.5, 8.0), dpi=300, sharex=True, gridspec_kw={"height_ratios": [2.1, 1.0]}
    )
    if sum_df.empty:
        plt.close(fig)
        return

    df = sum_df.copy()
    if "step_label" in df.columns:
        df["burst_size"] = df["step_label"].apply(
            lambda s: int(re.search(r"burst_([0-9]+)", str(s)).group(1)) if re.search(r"burst_([0-9]+)", str(s)) else 0
        )
        df = df.sort_values("burst_size")

    x = df.get("burst_size", pd.Series(range(len(df)))).to_numpy(dtype=float)
    p50_s = (df.get("p50_e2e_ms", pd.Series([0.0] * len(df))).fillna(0) / 1000.0).to_numpy(dtype=float)
    p95_s = (df.get("p95_e2e_ms", pd.Series([0.0] * len(df))).fillna(0) / 1000.0).to_numpy(dtype=float)
    drain_s = (df.get("max_e2e_ms", df.get("p95_e2e_ms", pd.Series([0.0] * len(df)))).fillna(0) / 1000.0).to_numpy(dtype=float)

    ax_top.plot(x, p50_s, marker="o", color="#0284c7", linewidth=2.2, label="Median p50 Latency (s)")
    ax_top.plot(x, p95_s, marker="s", color="#dc2626", linewidth=2.4, label="Tail p95 Latency (s)")
    if np.any(drain_s > 0):
        ax_top.plot(x, drain_s, marker="^", linestyle="--", color="#7c3aed", linewidth=2.2, label="Total Burst Drain Time (100% Done, s)")

    ax_top.set_ylabel("Time / Latency (seconds)")
    _place_legend_above(
        ax_top, ncol=3, fontsize=8.5,
        title="Classroom Deadline Burst Absorption, Total Drain Time & 2-Node Spillover",
    )
    ax_top.grid(True, linestyle="--", alpha=0.7)

    prim = df.get("primary_requests", pd.Series([0] * len(df))).fillna(0).to_numpy(dtype=float)
    ovf = df.get("overflow_requests", pd.Series([0] * len(df))).fillna(0).to_numpy(dtype=float)
    tot_llm = np.maximum(1.0, prim + ovf)
    ovf_pct = (ovf / tot_llm) * 100.0
    succ_pct = df.get("success_rate", pd.Series([1.0] * len(df))).fillna(1.0).to_numpy(dtype=float) * 100.0

    width = max(4.0, (float(x.max() - x.min()) / max(1, len(x))) * 0.35) if len(x) > 1 else 6.0
    ax_bot.bar(x, ovf_pct, width=width, color="#f97316", alpha=0.85, label="Overflow GPU Share (%)")
    ax_bot.plot(x, succ_pct, marker="o", color="#16a34a", linewidth=2.0, label="Success Rate (%)")
    ax_bot.set_ylim(0, 108)
    ax_bot.set_ylabel("Percentage (%)")
    ax_bot.set_xlabel("Burst Size (Simultaneous Submissions)")
    _place_legend_above(ax_bot, ncol=2, fontsize=8.5)
    ax_bot.grid(True, linestyle="--", alpha=0.7)

    plt.tight_layout(h_pad=2.2)
    fig.savefig(output_path, bbox_inches="tight", pad_inches=0.2)
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
    if suite_name == "ramp_capacity":
        p_ramp = output_dir / "spillover_saturation_ramp.png"
        p_stage = output_dir / "pipeline_latency_breakdown_stacked.png"
        plot_ramp_spillover_capacity(sub_df, sum_df, p_ramp)
        plot_obj1_bottleneck_stacked(sub_df, sum_df, p_stage)
        generated.extend([p_ramp, p_stage])
    elif suite_name in ("sla_curve", "obj4_sla_curve", "obj1_bottleneck", "custom"):
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
        p_stage = output_dir / "pipeline_latency_breakdown_stacked.png"
        plot_obj2_infinite_gpu_scaling(sub_df, sum_df, p2)
        plot_obj1_bottleneck_stacked(sub_df, sum_df, p_stage)
        generated.extend([p2, p_stage])
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
