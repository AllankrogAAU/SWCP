"""CLI entrypoint for SWCP benchmarking and stress-testing suite."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from benchmarks.config import (
    SUITE_ALIASES,
    BenchmarkConfig,
    load_suites_config,
)
from benchmarks.plots import generate_all_plots, plot_obj3_vllm_tuning_matrix
from benchmarks.runner import ClientSubmissionResult, LoadRunner
from benchmarks.seeder import Seeder
from benchmarks.storage import BenchmarkStorage
from benchmarks.telemetry import StageTimings, TelemetryCollector

console = Console()
logger = logging.getLogger("benchmarks")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run_warmup(config: BenchmarkConfig, count: int = 3) -> int:
    console.print(f"[bold cyan]=== Sending {count} Warmup Submissions (JIT & CUDA Graph Initialization) ===[/bold cyan]")
    seeder = Seeder(config)
    tokens = await seeder.ensure_users_and_tokens(count=count)
    assignment_id = await seeder.resolve_assignment_id(tokens[0])
    runner = LoadRunner(config, tokens, assignment_id, "warmup")
    import httpx
    async with httpx.AsyncClient(base_url=config.target_url, timeout=90.0) as client:
        for i in range(1, count + 1):
            console.print(f" [Warmup {i}/{count}] Executing warmup submission...")
            res = await runner.execute_single_submission("warmup", client, archetype="pass", timeout_s=90.0)
            console.print(f"  -> Finished: status={res.final_status}, latency={res.client_e2e_ms:.1f}ms")
    console.print("[bold green]✓ Warmup completed. System is warm and ready for benchmark runs.[/bold green]\n")
    return 0


def _store_submission_and_step_metrics(
    storage: BenchmarkStorage,
    telemetry: TelemetryCollector,
    run_id: str,
    results: list[ClientSubmissionResult],
    step_windows: dict[str, tuple[float, float]],
    start_epoch: float,
    end_epoch: float,
    step_meta: dict[str, dict[str, Any]] | None = None,
) -> None:
    sub_ids = [r.submission_id for r in results]
    stages_by_sub = telemetry.collect_batch_submission_stages(sub_ids, start_epoch, end_epoch)

    # For tiny tests (<= 5 submissions), if batch missed any stage details, fallback to individual query
    if len(results) <= 5:
        for r in results:
            st = stages_by_sub.get(r.submission_id)
            if not st or (st.sandbox_exec_ms == 0.0 and st.llm_exec_ms == 0.0):
                stages_by_sub[r.submission_id] = telemetry.collect_submission_stages(
                    r.submission_id, start_epoch, end_epoch
                )

    records_by_step: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        stages = stages_by_sub.get(r.submission_id) or StageTimings(submission_id=r.submission_id)
        stages_by_sub[r.submission_id] = stages
        submit_edge_ms = (
            max(0.0, r.http_accepted_ms - stages.http_enqueue_ms)
            if stages.http_enqueue_ms > 0
            else 0.0
        )
        delivery_overhead_ms = (
            max(0.0, r.client_e2e_ms - submit_edge_ms - stages.pipeline_total_ms)
            if stages.pipeline_total_ms > 0
            else 0.0
        )
        known_stages_ms = (
            stages.http_enqueue_ms
            + stages.sandbox_queue_wait_ms
            + stages.sandbox_exec_ms
            + stages.llm_queue_wait_ms
            + stages.llm_exec_ms
        )
        orchestration_overhead_ms = (
            max(0.0, stages.pipeline_total_ms - known_stages_ms)
            if stages.pipeline_total_ms > 0
            else 0.0
        )
        rec = {
            "submission_id": r.submission_id,
            "run_id": run_id,
            "step_label": r.step_label,
            "user_id": r.token[:12],
            "fixture_type": r.fixture_type,
            "client_e2e_ms": r.client_e2e_ms,
            "client_submit_ms": r.http_accepted_ms,
            "http_enqueue_ms": stages.http_enqueue_ms or r.http_accepted_ms,
            "submit_edge_network_ms": submit_edge_ms,
            "sandbox_queue_wait_ms": stages.sandbox_queue_wait_ms,
            "sandbox_exec_ms": stages.sandbox_exec_ms,
            "llm_queue_wait_ms": stages.llm_queue_wait_ms,
            "llm_exec_ms": stages.llm_exec_ms,
            "orchestration_overhead_ms": orchestration_overhead_ms,
            "pipeline_total_ms": stages.pipeline_total_ms or r.client_e2e_ms,
            "delivery_overhead_ms": delivery_overhead_ms,
            "prompt_tokens": stages.prompt_tokens,
            "completion_tokens": stages.completion_tokens,
            "tokens_per_sec": stages.tokens_per_sec,
            "final_status": r.final_status,
            "terminal_via": r.terminal_via,
            "outcome": stages.outcome or r.terminal_via,
            "llm_target": stages.llm_target,
            "sandbox_node": stages.sandbox_node,
            "llm_worker_node": stages.llm_worker_node,
        }
        storage.insert_submission(rec)
        records_by_step.setdefault(r.step_label, []).append(rec)

    by_step: dict[str, list[ClientSubmissionResult]] = {}
    for r in results:
        by_step.setdefault(r.step_label, []).append(r)

    ordered_labels = list(step_windows.keys())
    for lbl in by_step.keys():
        if lbl not in step_windows:
            ordered_labels.append(lbl)

    for step_label in ordered_labels:
        step_subs = by_step.get(step_label, [])
        step_recs = records_by_step.get(step_label, [])
        step_start_s, step_end_s = step_windows.get(step_label, (start_epoch, end_epoch))
        step_duration_s = max(0.6, step_end_s - step_start_s)
        step_duration_min = step_duration_s / 60.0
        prom_snap = telemetry.collect_prometheus_snapshot(step_start_s, step_end_s)

        e2e_vals = [s.client_e2e_ms for s in step_subs]
        subs_stages = [stages_by_sub[s.submission_id] for s in step_subs if s.submission_id in stages_by_sub]
        loki_tok_rates = [st.tokens_per_sec for st in subs_stages if st.tokens_per_sec > 0]
        mean_tok_s = prom_snap.mean_tok_per_s if prom_snap.mean_tok_per_s > 0 else (
            float(np.mean(loki_tok_rates)) if loki_tok_rates else 0.0
        )
        loki_primary = sum(1 for st in subs_stages if st.llm_target in ("primary", "overflow_fallback"))
        loki_overflow = sum(1 for st in subs_stages if st.llm_target == "overflow")
        primary_reqs = loki_primary if (loki_primary + loki_overflow) > 0 else prom_snap.primary_requests
        overflow_reqs = loki_overflow if (loki_primary + loki_overflow) > 0 else prom_snap.overflow_requests
        primary_exec_vals = [
            st.llm_exec_ms for st in subs_stages
            if st.llm_target in ("primary", "overflow_fallback") and st.llm_exec_ms > 0
        ]
        overflow_exec_vals = [
            st.llm_exec_ms for st in subs_stages
            if st.llm_target == "overflow" and st.llm_exec_ms > 0
        ]

        meta = (step_meta or {}).get(step_label, {})
        m_rate = re.findall(r"([0-9]+(?:\.[0-9]+)?)", step_label)
        default_target = float(m_rate[-1]) if m_rate else float(len(step_subs))
        target_load_val = float(meta.get("target_load", default_target))
        achieved_rpm = float(meta.get("achieved_throughput_per_min", len(step_subs) / step_duration_min))
        in_flight_end = int(meta.get("in_flight_end", 0))
        backlog_growth_rpm = float(meta.get("backlog_growth_per_min", 0.0))
        actual_dur_s = float(meta.get("step_duration_actual_s", step_duration_s))
        max_e2e_ms = float(meta.get("max_e2e_ms", max(e2e_vals) if e2e_vals else 0.0))
        is_sustained = int(meta.get("is_sustained", 1 if backlog_growth_rpm <= max(4.0, 0.15 * target_load_val) else 0))

        n_subs = len(step_subs)
        storage.insert_step_summary({
            "run_id": run_id,
            "step_label": step_label,
            "target_load": target_load_val,
            "achieved_throughput_per_min": achieved_rpm,
            "total_requests": n_subs,
            "success_rate": (sum(1 for s in step_subs if s.final_status == "COMPLETED") / n_subs) if n_subs else 1.0,
            "p50_e2e_ms": float(np.percentile(e2e_vals, 50)) if e2e_vals else 0.0,
            "p90_e2e_ms": float(np.percentile(e2e_vals, 90)) if e2e_vals else 0.0,
            "p95_e2e_ms": float(np.percentile(e2e_vals, 95)) if e2e_vals else 0.0,
            "p99_e2e_ms": float(np.percentile(e2e_vals, 99)) if e2e_vals else 0.0,
            "max_e2e_ms": max_e2e_ms,
            "in_flight_end": in_flight_end,
            "backlog_growth_per_min": backlog_growth_rpm,
            "step_duration_actual_s": actual_dur_s,
            "is_sustained": is_sustained,
            "mean_enqueue_ms": float(np.mean([rec["http_enqueue_ms"] for rec in step_recs])) if step_recs else 0.0,
            "p95_submit_edge_ms": float(np.percentile([rec["submit_edge_network_ms"] for rec in step_recs], 95)) if step_recs else 0.0,
            "p95_sandbox_wait_ms": float(np.percentile([st.sandbox_queue_wait_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_sandbox_exec_ms": float(np.percentile([st.sandbox_exec_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_llm_wait_ms": float(np.percentile([st.llm_queue_wait_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_llm_exec_ms": float(np.percentile([st.llm_exec_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_llm_exec_primary_ms": float(np.percentile(primary_exec_vals, 95)) if primary_exec_vals else 0.0,
            "p95_llm_exec_overflow_ms": float(np.percentile(overflow_exec_vals, 95)) if overflow_exec_vals else 0.0,
            "p95_orchestration_overhead_ms": float(np.percentile([rec["orchestration_overhead_ms"] for rec in step_recs], 95)) if step_recs else 0.0,
            "p95_delivery_overhead_ms": float(np.percentile([rec["delivery_overhead_ms"] for rec in step_recs], 95)) if step_recs else 0.0,
            "ws_completion_rate": (sum(1 for s in step_subs if s.terminal_via == "websocket") / n_subs) if n_subs else 1.0,
            "peak_kv_cache_pct": prom_snap.peak_kv_cache_pct,
            "primary_kv_cache_pct": prom_snap.primary_kv_cache_pct,
            "overflow_kv_cache_pct": prom_snap.overflow_kv_cache_pct,
            "mean_tok_per_s": mean_tok_s,
            "primary_requests": primary_reqs,
            "overflow_requests": overflow_reqs,
            "max_replicas_sandbox": prom_snap.max_replicas_sandbox,
            "max_replicas_llm_worker": prom_snap.max_replicas_llm_worker,
            "max_replicas_core_api": prom_snap.max_replicas_core_api,
            "max_replicas_sandbox_worker": prom_snap.max_replicas_sandbox_worker,
            "max_replicas_llm_worker_worker": prom_snap.max_replicas_llm_worker_worker,
            "max_replicas_core_api_worker": prom_snap.max_replicas_core_api_worker,
        })


async def _run_smoke_test(config: BenchmarkConfig) -> int:
    console.print("[bold cyan]=== SWCP Benchmark Smoke Test ===[/bold cyan]")
    now_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    run_id = f"{now_str}_smoke_{uuid.uuid4().hex[:6]}"
    storage = BenchmarkStorage(config)
    telemetry = TelemetryCollector(config)
    target_http_rtt_ms, clock_offset_s = telemetry.measure_target_baseline()
    console.print(
        f"[cyan]Target baseline: HTTP RTT={target_http_rtt_ms:.2f}ms, clock_offset={clock_offset_s:+.2f}s"
        + (f", inter_node_rtt={config.inter_node_rtt_ms:.2f}ms" if config.inter_node_rtt_ms is not None else "")
        + "[/cyan]"
    )

    console.print("[yellow]Phase A: Ensuring virtual test users and caching JWTs...[/yellow]")
    seeder = Seeder(config)
    tokens = await seeder.ensure_users_and_tokens(count=3)
    assignment_id = await seeder.resolve_assignment_id(tokens[0])
    console.print(f"[green]✓ Authenticated with {len(tokens)} virtual users on assignment {assignment_id}[/green]")

    start_epoch = time.time()
    start_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.insert_run({
        "run_id": run_id,
        "suite_name": "smoke_test",
        "hardware_tag": config.hardware_tag,
        "started_at_utc": start_utc,
        "ended_at_utc": None,
        "llm_model": "qwen2.5-coder-14b-instruct-awq",
        "vllm_gpu_mem_util": 0.80,
        "vllm_max_num_seqs": 4,
        "vllm_kv_cache_dtype": "auto",
        "mock_llm_enabled": 0 if not config.mock_llm_enabled else 1,
        "arrival_mode": "smoke",
        "target_rate_per_min": 2.0,
        "concurrent_users": 1,
        "cold_start": 0,
        "target_url": config.target_url,
        "target_http_rtt_ms": target_http_rtt_ms,
        "inter_node_rtt_ms": config.inter_node_rtt_ms,
        "clock_offset_s": clock_offset_s,
        "notes": "Minimal verification smoke test",
    })

    runner = LoadRunner(config, tokens, assignment_id, run_id)
    step_label = "smoke_step"
    results: list[ClientSubmissionResult] = []

    console.print("\n[yellow]Phase B: Executing 2 test submissions (Passing & Compile Error)...[/yellow]")
    import httpx
    async with httpx.AsyncClient(base_url=config.target_url, timeout=30.0) as client:
        console.print("Submitting Archetype 1: clean_primes (Passing)...")
        r1 = await runner.execute_single_submission(step_label, client, archetype="pass", timeout_s=90.0)
        results.append(r1)
        console.print(f" -> Result: status={r1.final_status}, e2e={r1.client_e2e_ms:.1f}ms, via={r1.terminal_via}")

        console.print("Submitting Archetype 2: broken_syntax (Compile Error)...")
        r2 = await runner.execute_single_submission(step_label, client, archetype="compile_error", timeout_s=90.0)
        results.append(r2)
        console.print(f" -> Result: status={r2.final_status}, e2e={r2.client_e2e_ms:.1f}ms, via={r2.terminal_via}")

    end_epoch = time.time()
    end_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.update_run_ended(run_id, end_utc)

    console.print("\n[yellow]Phase C: Querying Loki logs and Prometheus metrics...[/yellow]")
    await asyncio.sleep(2.0)
    _store_submission_and_step_metrics(
        storage, telemetry, run_id, results, {step_label: (start_epoch, end_epoch)}, start_epoch, end_epoch
    )

    console.print("\n[yellow]Phase D: Exporting CSV artifacts and generating charts...[/yellow]")
    run_dir = config.results_dir / run_id
    storage.export_artifacts(run_id, run_dir)
    plots = generate_all_plots(storage.db_path, run_id, run_dir, suite_name="smoke_test")

    console.print(f"[bold green]✓ Smoke test complete![/bold green] Output artifacts in: [cyan]{run_dir}[/cyan]")
    for p in plots:
        console.print(f"  - Chart generated: [underline]{p.name}[/underline]")

    table = Table(title=f"Smoke Test Results: {run_id}")
    table.add_column("Submission ID", style="cyan")
    table.add_column("Archetype", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Client E2E (ms)", justify="right")
    table.add_column("Terminal Via")
    for r in results:
        table.add_row(r.submission_id[:8] + "...", r.fixture_type, r.final_status, f"{r.client_e2e_ms:.1f}", r.terminal_via)
    console.print(table)

    return 0


async def _run_benchmark(config: BenchmarkConfig, args: argparse.Namespace) -> int:
    console.print(f"[bold cyan]=== Starting SWCP Benchmark Suite: {args.suite} ===[/bold cyan]")
    now_str = datetime.datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
    run_id = f"{now_str}_{args.suite}_{uuid.uuid4().hex[:6]}"
    storage = BenchmarkStorage(config)
    telemetry = TelemetryCollector(config)
    target_http_rtt_ms, clock_offset_s = telemetry.measure_target_baseline()
    console.print(
        f"[cyan]Target baseline: HTTP RTT={target_http_rtt_ms:.2f}ms, clock_offset={clock_offset_s:+.2f}s"
        + (f", inter_node_rtt={config.inter_node_rtt_ms:.2f}ms" if config.inter_node_rtt_ms is not None else "")
        + "[/cyan]"
    )
    seeder = Seeder(config)

    console.print(f"[yellow]Ensuring {config.user_pool_size} virtual test users...[/yellow]")
    tokens = await seeder.ensure_users_and_tokens()
    assignment_id = await seeder.resolve_assignment_id(tokens[0])
    console.print(f"[green]✓ Authenticated with {len(tokens)} virtual users[/green]")

    vllm_gpu_mem = float(getattr(args, "vllm_gpu_mem", None) or 0.80)
    vllm_max_seqs = int(getattr(args, "vllm_max_seqs", None) or 4)
    sweep_id = getattr(args, "sweep_id", None) or ""
    skip_plots = bool(getattr(args, "skip_plots", False) or sweep_id)

    if getattr(args, "warmup", False):
        await _run_warmup(config, count=3)
        await asyncio.sleep(1.0)

    start_epoch = time.time()
    start_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.insert_run({
        "run_id": run_id,
        "suite_name": args.suite,
        "hardware_tag": config.hardware_tag,
        "started_at_utc": start_utc,
        "ended_at_utc": None,
        "llm_model": "qwen2.5-coder-14b-instruct-awq",
        "vllm_gpu_mem_util": vllm_gpu_mem,
        "vllm_max_num_seqs": vllm_max_seqs,
        "vllm_kv_cache_dtype": "auto",
        "mock_llm_enabled": 1 if config.mock_llm_enabled else 0,
        "arrival_mode": args.arrival_mode,
        "target_rate_per_min": 0.0,
        "concurrent_users": 0,
        "cold_start": 1 if getattr(args, "cold_start", False) else 0,
        "target_url": config.target_url,
        "target_http_rtt_ms": target_http_rtt_ms,
        "inter_node_rtt_ms": config.inter_node_rtt_ms,
        "clock_offset_s": clock_offset_s,
        "notes": sweep_id or config.notes,
    })

    runner = LoadRunner(config, tokens, assignment_id, run_id)
    results: list[ClientSubmissionResult] = []

    if args.arrival_mode == "continuous-ramp":
        results = await runner.run_continuous_ramp(
            start_rate=float(args.start_rate),
            end_rate=float(args.end_rate),
            ramp_duration_s=float(args.ramp_duration_s),
            bucket_duration_s=float(args.bucket_duration_s),
            stop_on_saturation=bool(args.stop_on_saturation),
            saturation_confirm_buckets=int(args.saturation_confirm_buckets),
            saturation_deficit_ratio=float(args.saturation_deficit_ratio),
        )
    elif args.arrival_mode == "adaptive-poisson":
        results = await runner.run_adaptive_poisson(
            start_rate=float(args.start_rate),
            rate_delta=float(args.rate_delta),
            max_rate=float(args.max_rate or 0.0),
            target_p95_s=float(args.target_p95_s),
            step_duration_s=float(args.step_duration_s),
            min_completions=int(args.min_completions),
            confirmation_steps=int(args.confirmation_steps),
        )
    elif args.arrival_mode == "stepped-poisson":
        rates = [float(r.strip()) for r in str(args.rates).split(",") if r.strip()]
        results = await runner.run_stepped_poisson(
            rates_per_minute=rates,
            step_duration_s=float(args.step_duration_s),
            min_completions=int(args.min_completions),
        )
    elif args.arrival_mode == "closed-loop":
        concurrencies = [int(c.strip()) for c in str(getattr(args, "concurrency", "")).split(",") if c.strip()]
        results = await runner.run_closed_loop(
            concurrency_levels=concurrencies,
            step_duration_s=float(args.step_duration_s),
            think_time_min_s=float(args.think_time_min_s),
            think_time_max_s=float(args.think_time_max_s),
        )
    elif args.arrival_mode == "burst":
        bursts = [int(b.strip()) for b in str(args.bursts).split(",") if b.strip()]
        results = await runner.run_burst(
            burst_sizes=bursts,
            burst_window_s=float(args.burst_window_s),
            drain_timeout_s=float(args.drain_timeout_s),
        )

    end_epoch = time.time()
    end_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.update_run_ended(run_id, end_utc)

    console.print("\n[yellow]Collecting telemetry from cluster Loki & Prometheus...[/yellow]")
    await asyncio.sleep(2.0)
    _store_submission_and_step_metrics(
        storage, telemetry, run_id, results, runner.step_windows, start_epoch, end_epoch,
        step_meta=runner.step_meta,
    )

    if skip_plots:
        console.print(f"[bold green]✓ Recorded sweep step ({run_id}) into SQLite database.[/bold green]")
        return 0

    run_dir = config.results_dir / run_id
    storage.export_artifacts(run_id, run_dir)
    plots = generate_all_plots(storage.db_path, run_id, run_dir, suite_name=args.suite)

    console.print(f"[bold green]✓ Benchmark run completed![/bold green] Results saved to: [cyan]{run_dir}[/cyan]")
    for p in plots:
        console.print(f"  - Chart: [underline]{p.name}[/underline]")

    return 0


def _run_sweep_report(config: BenchmarkConfig, sweep_id: str | None = None) -> int:
    storage = BenchmarkStorage(config)
    if not storage.db_path.exists():
        console.print("[red]No benchmarks.sqlite database found yet.[/red]")
        return 1

    query = """
        SELECT r.run_id,
               r.hardware_tag,
               r.vllm_gpu_mem_util,
               r.vllm_max_num_seqs,
               r.started_at_utc,
               r.notes,
               s.total_requests,
               s.success_rate,
               s.achieved_throughput_per_min,
               s.mean_tok_per_s,
               s.peak_kv_cache_pct,
               s.p50_e2e_ms,
               s.p90_e2e_ms,
               s.p95_e2e_ms,
               s.p95_llm_wait_ms,
               s.p95_llm_exec_ms
        FROM step_summaries s
        JOIN runs r ON r.run_id = s.run_id
        WHERE r.suite_name IN ('obj3_vllm_sweep', 'vllm_sweep')
    """
    params: list[Any] = []
    if sweep_id:
        query += " AND r.notes = ?"
        params.append(sweep_id)
    query += " ORDER BY r.started_at_utc ASC"

    with sqlite3.connect(storage.db_path) as conn:
        df = pd.read_sql_query(query, conn, params=params)

    if df.empty and sweep_id:
        # Fallback to all obj3_vllm_sweep runs if sweep_id had no exact match
        with sqlite3.connect(storage.db_path) as conn:
            df = pd.read_sql_query(
                query.replace(" AND r.notes = ?", ""),
                conn,
            )

    if df.empty:
        console.print("[red]No vLLM sweep runs found in benchmarks.sqlite.[/red]")
        return 1

    # Extract MEM and SEQS from columns or hardware_tag (e.g. "rtx5070ti_mem0.88_seqs8")
    mem_list: list[float] = []
    seqs_list: list[int] = []
    labels: list[str] = []
    for _, row in df.iterrows():
        tag = str(row["hardware_tag"] or "")
        m = re.search(r"mem([0-9.]+)_seqs([0-9]+)", tag)
        if m:
            mem_val = float(m.group(1))
            seqs_val = int(m.group(2))
        else:
            mem_val = float(row["vllm_gpu_mem_util"] or 0.80)
            seqs_val = int(row["vllm_max_num_seqs"] or 4)
        mem_list.append(mem_val)
        seqs_list.append(seqs_val)
        labels.append(f"MEM={mem_val:.2f}, SEQS={seqs_val}")

    df["vllm_gpu_mem_util"] = mem_list
    df["vllm_max_num_seqs"] = seqs_list
    df["config_label"] = labels

    # Rank configurations: prefer high success_rate (>=0.90), then lowest p95_e2e_ms and highest throughput
    valid_df = df[df["success_rate"] >= 0.80].copy()
    if valid_df.empty:
        valid_df = df.copy()
    valid_df["score"] = (
        valid_df["achieved_throughput_per_min"] * 2.0
        + valid_df["mean_tok_per_s"] * 0.1
        - (valid_df["p95_e2e_ms"] / 1000.0)
    )
    winner = valid_df.sort_values(by=["score"], ascending=False).iloc[0]
    winner_run_id = str(winner["run_id"])

    table = Table(title="vLLM Hardware Parameter Sweep Comparison")
    table.add_column("GPU Mem Util", justify="right", style="cyan")
    table.add_column("Max Seqs", justify="right", style="cyan")
    table.add_column("Throughput (subs/min)", justify="right", style="green")
    table.add_column("Speed (tok/s)", justify="right", style="green")
    table.add_column("Peak KV (%)", justify="right", style="yellow")
    table.add_column("p50 E2E (s)", justify="right")
    table.add_column("p95 E2E (s)", justify="right", style="magenta")
    table.add_column("Success %", justify="right")
    table.add_column("Status", justify="center")

    for _, row in df.iterrows():
        is_winner = str(row["run_id"]) == winner_run_id
        badge = "[bold green]★ BEST[/bold green]" if is_winner else ""
        table.add_row(
            f"{row['vllm_gpu_mem_util']:.2f}",
            f"{int(row['vllm_max_num_seqs'])}",
            f"{row['achieved_throughput_per_min']:.2f}",
            f"{row['mean_tok_per_s']:.1f}",
            f"{row['peak_kv_cache_pct']:.1f}%",
            f"{row['p50_e2e_ms'] / 1000.0:.2f}s",
            f"{row['p95_e2e_ms'] / 1000.0:.2f}s",
            f"{row['success_rate'] * 100.0:.0f}%",
            badge,
        )

    console.print()
    console.print(table)
    console.print(
        f"\n[bold green]★ Recommended Optimal Setting:[/bold green] "
        f"[bold cyan]VLLM_GPU_MEMORY_UTILIZATION={winner['vllm_gpu_mem_util']:.2f}[/bold cyan], "
        f"[bold cyan]VLLM_MAX_NUM_SEQS={int(winner['vllm_max_num_seqs'])}[/bold cyan] "
        f"(p95={winner['p95_e2e_ms'] / 1000.0:.2f}s, throughput={winner['achieved_throughput_per_min']:.2f} subs/min)"
    )

    out_folder_name = sweep_id or "obj3_vllm_sweep_latest"
    out_dir = config.results_dir / out_folder_name
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "sweep_summary.csv"
    df.to_csv(csv_path, index=False)

    chart_path = out_dir / "obj3_vllm_tuning_matrix.png"
    plot_obj3_vllm_tuning_matrix(df, chart_path)

    winner_env_path = out_dir / "winner.env"
    with open(winner_env_path, "w", encoding="utf-8") as f:
        f.write(f"BEST_MEM={winner['vllm_gpu_mem_util']:.2f}\n")
        f.write(f"BEST_SEQS={int(winner['vllm_max_num_seqs'])}\n")

    console.print(f"[cyan]Saved consolidated sweep CSV & chart to:[/cyan] {out_dir}")
    console.print(f"  - [underline]{csv_path.name}[/underline]")
    console.print(f"  - [underline]{chart_path.name}[/underline]")
    return 0


def _get_latest_run_id(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        res = conn.execute("SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return res[0] if res else None


def main() -> None:
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--config", default=None, help="Path to suite configuration TOML (default: benchmarks/suites.toml)")
    parent_parser.add_argument("--target-url", default=None, help="Target Ingress URL (e.g. http://localhost)")
    parent_parser.add_argument("--prometheus-url", default=None, help="Cluster Prometheus URL")
    parent_parser.add_argument("--loki-url", default=None, help="Cluster Loki URL")
    parent_parser.add_argument("--results-dir", default=None, help="Directory to store sqlite DB, csv, and plots")
    parent_parser.add_argument("--users", type=int, default=None, help="User pool size (default from suites.toml or 100)")
    parent_parser.add_argument("--hardware-tag", default=None, help="Hardware tag for runs (default from suites.toml)")
    parent_parser.add_argument("--inter-node-rtt-ms", type=float, default=None, help="Measured baseline inter-node ping RTT (ms)")
    parent_parser.add_argument("--mock-llm", action="store_true", default=None, help="Set flag if running in infinite-GPU mock mode")
    parent_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    parser = argparse.ArgumentParser(description="SWCP Benchmark and Stress-Testing Suite", parents=[parent_parser])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # smoke-test
    subparsers.add_parser("smoke-test", parents=[parent_parser], help="Execute minimal 2-request verification test")

    # warmup
    warmup_parser = subparsers.add_parser("warmup", parents=[parent_parser], help="Send warmup requests to initialize JIT and CUDA graphs")
    warmup_parser.add_argument("--count", type=int, default=3, help="Number of warmup requests (default: 3)")

    # seed
    subparsers.add_parser("seed", parents=[parent_parser], help="Pre-seed virtual users and cache JWTs")

    # run
    run_parser = subparsers.add_parser("run", parents=[parent_parser], help="Run a benchmark suite")
    run_parser.add_argument(
        "--suite",
        required=True,
        choices=[
            "ramp_capacity", "sla_curve", "burst", "infinite_gpu", "vllm_sweep",
            "obj1_bottleneck", "obj2_infinite_gpu", "obj3_vllm_sweep", "obj4_sla_curve", "custom"
        ]
    )
    run_parser.add_argument(
        "--arrival-mode",
        default=None,
        choices=["continuous-ramp", "adaptive-poisson", "stepped-poisson", "closed-loop", "burst"],
        help="Arrival traffic model (defaults from benchmarks/suites.toml)"
    )
    run_parser.add_argument("--rates", default=None, help="Comma-separated rates/min for stepped-poisson")
    run_parser.add_argument("--start-rate", type=float, default=None, help="Initial arrival rate (rpm) for ramp/adaptive-poisson")
    run_parser.add_argument("--end-rate", type=float, default=None, help="Final arrival rate (rpm) for continuous-ramp")
    run_parser.add_argument("--ramp-duration-s", type=float, default=None, help="Total duration (s) for continuous-ramp")
    run_parser.add_argument("--bucket-duration-s", type=float, default=None, help="Bucket slice width (s) for continuous-ramp")
    run_parser.add_argument("--stop-on-saturation", dest="stop_on_saturation", action="store_true", default=None, help="Auto-stop continuous-ramp cleanly after confirming sustained saturation")
    run_parser.add_argument("--no-stop-on-saturation", dest="stop_on_saturation", action="store_false", help="Run full continuous-ramp duration without saturation early stop")
    run_parser.add_argument("--saturation-confirm-buckets", type=int, default=None, help="Consecutive saturated buckets required before stopping continuous-ramp")
    run_parser.add_argument("--saturation-deficit-ratio", type=float, default=None, help="Completion shortfall fraction vs target rate required for saturation (default: 0.12)")
    run_parser.add_argument("--rate-delta", type=float, default=None, help="Rate step increment for adaptive-poisson")
    run_parser.add_argument("--max-rate", type=float, default=None, help="Optional max rate ceiling for adaptive-poisson")
    run_parser.add_argument("--target-p95-s", type=float, default=None, help="Saturation threshold (s) for adaptive-poisson")
    run_parser.add_argument("--confirmation-steps", type=int, default=None, help="Steps to run after crossing target-p95-s")
    run_parser.add_argument("--concurrency", default=None, help="Comma-separated concurrent users for closed-loop")
    run_parser.add_argument("--think-time-min-s", type=float, default=None, help="Minimum think time (s) between closed-loop requests")
    run_parser.add_argument("--think-time-max-s", type=float, default=None, help="Maximum think time (s) between closed-loop requests")
    run_parser.add_argument("--bursts", default=None, help="Comma-separated burst sizes for burst mode")
    run_parser.add_argument("--step-duration-s", type=float, default=None, help="Duration of each step in seconds")
    run_parser.add_argument("--min-completions", type=int, default=None, help="Minimum completions per step")
    run_parser.add_argument("--burst-window-s", type=float, default=None, help="Window duration for burst mode in seconds")
    run_parser.add_argument("--drain-timeout-s", type=float, default=None, help="Max drain timeout per burst in seconds")
    run_parser.add_argument("--warmup", dest="warmup", action="store_true", default=None, help="Send 3 warmup requests before run")
    run_parser.add_argument("--no-warmup", dest="warmup", action="store_false", help="Skip pre-run warmup requests")
    run_parser.add_argument("--cold-start", action="store_true", help="Flag if testing cold start from min replicas")
    run_parser.add_argument("--vllm-gpu-mem", type=float, default=0.88, help="Configured VLLM_GPU_MEMORY_UTILIZATION")
    run_parser.add_argument("--vllm-max-seqs", type=int, default=4, help="Configured VLLM_MAX_NUM_SEQS")
    run_parser.add_argument("--sweep-id", default="", help="Group ID for a multi-point parameter sweep")
    run_parser.add_argument("--skip-plots", action="store_true", help="Record to SQLite only without per-run plots")

    # sweep-report
    sweep_parser = subparsers.add_parser("sweep-report", parents=[parent_parser], help="Print table and generate single plot for vLLM sweep")
    sweep_parser.add_argument("--sweep-id", default=None, help="Optional sweep ID filter (defaults to all obj3_vllm_sweep runs)")

    # plot
    plot_parser = subparsers.add_parser("plot", parents=[parent_parser], help="Generate plots for an existing run")
    plot_parser.add_argument("--run-id", default="latest", help="Run ID to plot (or 'latest')")

    # export
    export_parser = subparsers.add_parser("export", parents=[parent_parser], help="Export CSV and JSON artifacts for a run")
    export_parser.add_argument("--run-id", default="latest", help="Run ID to export (or 'latest')")

    args = parser.parse_args()
    setup_logging(args.verbose)

    suites_toml = load_suites_config(Path(args.config) if args.config else None)
    common_cfg = suites_toml.get("common", {})
    raw_suite = getattr(args, "suite", None) or "custom"
    canonical_suite = SUITE_ALIASES.get(raw_suite, raw_suite)
    suite_cfg = suites_toml.get("suites", {}).get(canonical_suite, {}) if args.command == "run" else {}

    fallback_defaults: dict[str, Any] = {
        "users": 100,
        "hardware_tag": "2node-rtx5070ti",
        "mock_llm": False,
        "arrival_mode": "stepped-poisson",
        "rates": "40,80,120,160,200,240",
        "start_rate": 30.0,
        "end_rate": 260.0,
        "ramp_duration_s": 480.0,
        "bucket_duration_s": 30.0,
        "stop_on_saturation": False,
        "saturation_confirm_buckets": 3,
        "saturation_deficit_ratio": 0.12,
        "rate_delta": 10.0,
        "max_rate": 0.0,
        "target_p95_s": 30.0,
        "confirmation_steps": 1,
        "concurrency": "16",
        "think_time_min_s": 10.0,
        "think_time_max_s": 20.0,
        "bursts": "20,40,60,80,100,120",
        "step_duration_s": 90.0,
        "min_completions": 12,
        "burst_window_s": 3.0,
        "drain_timeout_s": 300.0,
        "warmup": False,
    }

    for key, fb in fallback_defaults.items():
        if getattr(args, key, None) is None:
            if key in suite_cfg:
                setattr(args, key, suite_cfg[key])
            elif key in common_cfg:
                setattr(args, key, common_cfg[key])
            else:
                setattr(args, key, fb)

    if getattr(args, "sweep_id", "") and "--warmup" not in sys.argv:
        args.warmup = False

    config_kwargs: dict[str, Any] = {
        "hardware_tag": str(args.hardware_tag),
        "mock_llm_enabled": bool(args.mock_llm),
    }
    if args.target_url:
        config_kwargs["target_url"] = args.target_url
    if args.prometheus_url:
        config_kwargs["prometheus_url"] = args.prometheus_url
    if args.loki_url:
        config_kwargs["loki_url"] = args.loki_url
    if args.results_dir:
        config_kwargs["results_dir"] = Path(args.results_dir)
    if args.users:
        config_kwargs["user_pool_size"] = int(args.users)
    if args.inter_node_rtt_ms is not None:
        config_kwargs["inter_node_rtt_ms"] = args.inter_node_rtt_ms

    # Auto-scale user pool size if concurrency, burst, or high-rate arrivals could saturate pool
    mode = getattr(args, "arrival_mode", None)
    cur_pool = config_kwargs.get("user_pool_size", 200)
    if mode == "closed-loop":
        concurrencies = [int(c.strip()) for c in str(getattr(args, "concurrency", "")).split(",") if c.strip()]
        if concurrencies:
            config_kwargs["user_pool_size"] = max(cur_pool, max(concurrencies) + 20)
    elif mode == "burst":
        bursts = [int(b.strip()) for b in str(getattr(args, "bursts", "")).split(",") if b.strip()]
        if bursts:
            config_kwargs["user_pool_size"] = max(cur_pool, max(bursts) + 20)
    elif mode == "continuous-ramp":
        peak_rpm = float(getattr(args, "end_rate", 0.0) or 0.0)
        config_kwargs["user_pool_size"] = max(cur_pool, min(1000, int(peak_rpm * 1.0) + 100))
    elif mode == "stepped-poisson":
        rates = [float(r.strip()) for r in str(getattr(args, "rates", "")).split(",") if r.strip()]
        if rates:
            config_kwargs["user_pool_size"] = max(cur_pool, min(800, int(max(rates) * 1.5) + 50))
    elif mode == "adaptive-poisson":
        max_r = float(getattr(args, "max_rate", 0.0) or 0.0)
        config_kwargs["user_pool_size"] = max(cur_pool, min(800, max(300, int(max_r * 1.5))))

    config = BenchmarkConfig(**config_kwargs)

    if args.command == "smoke-test":
        sys.exit(asyncio.run(_run_smoke_test(config)))

    elif args.command == "warmup":
        sys.exit(asyncio.run(_run_warmup(config, args.count)))

    elif args.command == "seed":
        seeder = Seeder(config)
        tokens = asyncio.run(seeder.ensure_users_and_tokens())
        console.print(f"[bold green]✓ Seeded {len(tokens)} virtual users and cached tokens![/bold green]")
        sys.exit(0)

    elif args.command == "run":
        sys.exit(asyncio.run(_run_benchmark(config, args)))

    elif args.command == "sweep-report":
        sys.exit(_run_sweep_report(config, args.sweep_id))

    elif args.command == "plot":
        storage = BenchmarkStorage(config)
        target_run_id = args.run_id
        if target_run_id == "latest":
            target_run_id = _get_latest_run_id(storage.db_path)
            if not target_run_id:
                console.print("[red]No runs found in database to plot.[/red]")
                sys.exit(1)
        run_dir = config.results_dir / target_run_id
        plots = generate_all_plots(storage.db_path, target_run_id, run_dir)
        console.print(f"[bold green]✓ Generated {len(plots)} charts in: [cyan]{run_dir}[/cyan][/bold green]")
        sys.exit(0)

    elif args.command == "export":
        storage = BenchmarkStorage(config)
        target_run_id = args.run_id
        if target_run_id == "latest":
            target_run_id = _get_latest_run_id(storage.db_path)
            if not target_run_id:
                console.print("[red]No runs found in database to export.[/red]")
                sys.exit(1)
        run_dir = config.results_dir / target_run_id
        storage.export_artifacts(target_run_id, run_dir)
        console.print(f"[bold green]✓ Exported artifacts for {target_run_id} to: [cyan]{run_dir}[/cyan][/bold green]")
        sys.exit(0)


if __name__ == "__main__":
    main()
