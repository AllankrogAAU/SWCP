"""CLI entrypoint for SWCP benchmarking and stress-testing suite."""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
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

from benchmarks.config import BenchmarkConfig
from benchmarks.plots import generate_all_plots
from benchmarks.runner import ClientSubmissionResult, LoadRunner
from benchmarks.seeder import Seeder
from benchmarks.storage import BenchmarkStorage
from benchmarks.telemetry import TelemetryCollector

console = Console()
logger = logging.getLogger("benchmarks")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run_smoke_test(config: BenchmarkConfig) -> int:
    console.print("[bold cyan]=== SWCP Benchmark Smoke Test ===[/bold cyan]")
    run_id = f"smoke_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    storage = BenchmarkStorage(config)
    telemetry = TelemetryCollector(config)

    # 1. Pre-seed minimal users
    console.print("[yellow]Phase A: Ensuring virtual test users and caching JWTs...[/yellow]")
    seeder = Seeder(config)
    tokens = await seeder.ensure_users_and_tokens(count=3)
    assignment_id = await seeder.resolve_assignment_id(tokens[0])
    console.print(f"[green]✓ Authenticated with {len(tokens)} virtual users on assignment {assignment_id}[/green]")

    # 2. Record run in SQLite
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
        "notes": "Minimal verification smoke test",
    })

    # 3. Execute 2 distinct archetypes: Clean Passing Primes and Syntax Error
    runner = LoadRunner(config, tokens, assignment_id, run_id)
    step_label = "smoke_step"
    results: list[ClientSubmissionResult] = []

    console.print("\n[yellow]Phase B: Executing 2 test submissions (Passing & Compile Error)...[/yellow]")
    import httpx
    async with httpx.AsyncClient(base_url=config.target_url, timeout=30.0) as client:
        # Submission 1: clean primes
        console.print("Submitting Archetype 1: clean_primes (Passing)...")
        r1 = await runner.execute_single_submission(step_label, client, archetype="pass", timeout_s=90.0)
        results.append(r1)
        console.print(f" -> Result: status={r1.final_status}, e2e={r1.client_e2e_ms:.1f}ms, via={r1.terminal_via}")

        # Submission 2: broken syntax
        console.print("Submitting Archetype 2: broken_syntax (Compile Error)...")
        r2 = await runner.execute_single_submission(step_label, client, archetype="compile_error", timeout_s=90.0)
        results.append(r2)
        console.print(f" -> Result: status={r2.final_status}, e2e={r2.client_e2e_ms:.1f}ms, via={r2.terminal_via}")

    end_epoch = time.time()
    end_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.update_run_ended(run_id, end_utc)

    # 4. Telemetry collection
    console.print("\n[yellow]Phase C: Querying Loki logs and Prometheus metrics...[/yellow]")
    await asyncio.sleep(2.0)  # Brief delay for Loki ingestion flush

    collected_stages = []
    for r in results:
        stages = telemetry.collect_submission_stages(r.submission_id, start_epoch, end_epoch)
        collected_stages.append(stages)
        storage.insert_submission({
            "submission_id": r.submission_id,
            "run_id": run_id,
            "step_label": r.step_label,
            "user_id": r.token[:12],
            "fixture_type": r.fixture_type,
            "client_e2e_ms": r.client_e2e_ms,
            "http_enqueue_ms": stages.http_enqueue_ms or r.http_accepted_ms,
            "sandbox_queue_wait_ms": stages.sandbox_queue_wait_ms,
            "sandbox_exec_ms": stages.sandbox_exec_ms,
            "llm_queue_wait_ms": stages.llm_queue_wait_ms,
            "llm_exec_ms": stages.llm_exec_ms,
            "pipeline_total_ms": stages.pipeline_total_ms or r.client_e2e_ms,
            "prompt_tokens": stages.prompt_tokens,
            "completion_tokens": stages.completion_tokens,
            "tokens_per_sec": stages.tokens_per_sec,
            "final_status": r.final_status,
            "outcome": stages.outcome or r.terminal_via,
        })

    prom_snap = telemetry.collect_prometheus_snapshot(start_epoch, end_epoch)

    # Step summary
    e2e_vals = [r.client_e2e_ms for r in results]
    storage.insert_step_summary({
        "run_id": run_id,
        "step_label": step_label,
        "target_load": 2.0,
        "achieved_throughput_per_min": len(results) / ((end_epoch - start_epoch) / 60.0),
        "total_requests": len(results),
        "success_rate": sum(1 for r in results if r.final_status == "COMPLETED") / len(results),
        "p50_e2e_ms": float(np.percentile(e2e_vals, 50)),
        "p90_e2e_ms": float(np.percentile(e2e_vals, 90)),
        "p95_e2e_ms": float(np.percentile(e2e_vals, 95)),
        "p99_e2e_ms": float(np.percentile(e2e_vals, 99)),
        "mean_enqueue_ms": float(np.mean([r.http_accepted_ms for r in results])),
        "p95_sandbox_wait_ms": float(np.percentile([s.sandbox_queue_wait_ms for s in collected_stages], 95)) if collected_stages else 0.0,
        "p95_sandbox_exec_ms": float(np.percentile([s.sandbox_exec_ms for s in collected_stages], 95)) if collected_stages else 0.0,
        "p95_llm_wait_ms": float(np.percentile([s.llm_queue_wait_ms for s in collected_stages], 95)) if collected_stages else 0.0,
        "p95_llm_exec_ms": float(np.percentile([s.llm_exec_ms for s in collected_stages], 95)) if collected_stages else 0.0,
        "peak_kv_cache_pct": prom_snap.peak_kv_cache_pct,
        "mean_tok_per_s": prom_snap.mean_tok_per_s,
        "max_replicas_sandbox": prom_snap.max_replicas_sandbox,
        "max_replicas_llm_worker": prom_snap.max_replicas_llm_worker,
        "max_replicas_core_api": prom_snap.max_replicas_core_api,
    })

    # 5. Export artifacts and generate plots
    console.print("\n[yellow]Phase D: Exporting CSV artifacts and generating 4 objective charts...[/yellow]")
    run_dir = config.results_dir / run_id
    storage.export_artifacts(run_id, run_dir)
    plots = generate_all_plots(storage.db_path, run_id, run_dir)

    console.print(f"[bold green]✓ Smoke test complete![/bold green] Output artifacts in: [cyan]{run_dir}[/cyan]")
    for p in plots:
        console.print(f"  - Chart generated: [underline]{p.name}[/underline]")

    # Print summary table
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
    run_id = f"{args.suite}_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    storage = BenchmarkStorage(config)
    telemetry = TelemetryCollector(config)
    seeder = Seeder(config)

    # Pre-seed users
    console.print(f"[yellow]Ensuring {config.user_pool_size} virtual test users...[/yellow]")
    tokens = await seeder.ensure_users_and_tokens()
    assignment_id = await seeder.resolve_assignment_id(tokens[0])
    console.print(f"[green]✓ Authenticated with {len(tokens)} virtual users[/green]")

    start_epoch = time.time()
    start_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.insert_run({
        "run_id": run_id,
        "suite_name": args.suite,
        "hardware_tag": config.hardware_tag,
        "started_at_utc": start_utc,
        "ended_at_utc": None,
        "llm_model": "qwen2.5-coder-14b-instruct-awq",
        "vllm_gpu_mem_util": 0.80,
        "vllm_max_num_seqs": 4,
        "vllm_kv_cache_dtype": "auto",
        "mock_llm_enabled": 1 if config.mock_llm_enabled else 0,
        "arrival_mode": args.arrival_mode,
        "target_rate_per_min": 0.0,
        "concurrent_users": 0,
        "cold_start": 1 if getattr(args, "cold_start", False) else 0,
        "notes": config.notes,
    })

    runner = LoadRunner(config, tokens, assignment_id, run_id)
    results: list[ClientSubmissionResult] = []

    if args.arrival_mode == "stepped-poisson":
        rates = [float(r.strip()) for r in args.rates.split(",") if r.strip()]
        results = await runner.run_stepped_poisson(
            rates_per_minute=rates,
            step_duration_s=args.step_duration_s,
            min_completions=args.min_completions,
        )
    elif args.arrival_mode == "closed-loop":
        concurrencies = [int(c.strip()) for c in args.concurrency.split(",") if c.strip()]
        results = await runner.run_closed_loop(
            concurrency_levels=concurrencies,
            step_duration_s=args.step_duration_s,
        )
    elif args.arrival_mode == "burst":
        bursts = [int(b.strip()) for b in args.bursts.split(",") if b.strip()]
        results = await runner.run_burst(
            burst_sizes=bursts,
            burst_window_s=args.burst_window_s,
        )

    end_epoch = time.time()
    end_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    storage.update_run_ended(run_id, end_utc)

    # Scrape Loki & Prometheus
    console.print("\n[yellow]Collecting telemetry from cluster Loki & Prometheus...[/yellow]")
    await asyncio.sleep(2.0)
    stages_by_sub: dict[str, StageTimings] = {}
    for r in results:
        stages = telemetry.collect_submission_stages(r.submission_id, start_epoch, end_epoch)
        stages_by_sub[r.submission_id] = stages
        storage.insert_submission({
            "submission_id": r.submission_id,
            "run_id": run_id,
            "step_label": r.step_label,
            "user_id": r.token[:12],
            "fixture_type": r.fixture_type,
            "client_e2e_ms": r.client_e2e_ms,
            "http_enqueue_ms": stages.http_enqueue_ms or r.http_accepted_ms,
            "sandbox_queue_wait_ms": stages.sandbox_queue_wait_ms,
            "sandbox_exec_ms": stages.sandbox_exec_ms,
            "llm_queue_wait_ms": stages.llm_queue_wait_ms,
            "llm_exec_ms": stages.llm_exec_ms,
            "pipeline_total_ms": stages.pipeline_total_ms or r.client_e2e_ms,
            "prompt_tokens": stages.prompt_tokens,
            "completion_tokens": stages.completion_tokens,
            "tokens_per_sec": stages.tokens_per_sec,
            "final_status": r.final_status,
            "outcome": stages.outcome or r.terminal_via,
        })

    prom_snap = telemetry.collect_prometheus_snapshot(start_epoch, end_epoch)

    # Aggregate summaries by step
    by_step: dict[str, list[ClientSubmissionResult]] = {}
    for r in results:
        by_step.setdefault(r.step_label, []).append(r)

    for step_label, step_subs in by_step.items():
        e2e_vals = [s.client_e2e_ms for s in step_subs]
        subs_stages = [stages_by_sub[s.submission_id] for s in step_subs if s.submission_id in stages_by_sub]
        storage.insert_step_summary({
            "run_id": run_id,
            "step_label": step_label,
            "target_load": float(len(step_subs)),
            "achieved_throughput_per_min": len(step_subs) / max(0.1, (end_epoch - start_epoch) / 60.0),
            "total_requests": len(step_subs),
            "success_rate": sum(1 for s in step_subs if s.final_status == "COMPLETED") / len(step_subs),
            "p50_e2e_ms": float(np.percentile(e2e_vals, 50)),
            "p90_e2e_ms": float(np.percentile(e2e_vals, 90)),
            "p95_e2e_ms": float(np.percentile(e2e_vals, 95)),
            "p99_e2e_ms": float(np.percentile(e2e_vals, 99)),
            "mean_enqueue_ms": float(np.mean([s.http_accepted_ms for s in step_subs])),
            "p95_sandbox_wait_ms": float(np.percentile([st.sandbox_queue_wait_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_sandbox_exec_ms": float(np.percentile([st.sandbox_exec_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_llm_wait_ms": float(np.percentile([st.llm_queue_wait_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "p95_llm_exec_ms": float(np.percentile([st.llm_exec_ms for st in subs_stages], 95)) if subs_stages else 0.0,
            "peak_kv_cache_pct": prom_snap.peak_kv_cache_pct,
            "mean_tok_per_s": prom_snap.mean_tok_per_s,
            "max_replicas_sandbox": prom_snap.max_replicas_sandbox,
            "max_replicas_llm_worker": prom_snap.max_replicas_llm_worker,
            "max_replicas_core_api": prom_snap.max_replicas_core_api,
        })

    # Export artifacts & plots
    run_dir = config.results_dir / run_id
    storage.export_artifacts(run_id, run_dir)
    plots = generate_all_plots(storage.db_path, run_id, run_dir)

    console.print(f"[bold green]✓ Benchmark run completed![/bold green] Results saved to: [cyan]{run_dir}[/cyan]")
    for p in plots:
        console.print(f"  - Chart: [underline]{p.name}[/underline]")

    return 0


def _get_latest_run_id(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    with sqlite3.connect(db_path) as conn:
        res = conn.execute("SELECT run_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return res[0] if res else None


def main() -> None:
    parent_parser = argparse.ArgumentParser(add_help=False)
    parent_parser.add_argument("--target-url", default=None, help="Target Ingress URL (e.g. http://localhost)")
    parent_parser.add_argument("--prometheus-url", default=None, help="Cluster Prometheus URL")
    parent_parser.add_argument("--loki-url", default=None, help="Cluster Loki URL")
    parent_parser.add_argument("--results-dir", default=None, help="Directory to store sqlite DB, csv, and plots")
    parent_parser.add_argument("--users", type=int, default=50, help="User pool size")
    parent_parser.add_argument("--hardware-tag", default="rtx5070ti-16gb", help="Hardware tag for runs")
    parent_parser.add_argument("--mock-llm", action="store_true", help="Set flag if running in infinite-GPU mock mode")
    parent_parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")

    parser = argparse.ArgumentParser(description="SWCP Benchmark and Stress-Testing Suite", parents=[parent_parser])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # smoke-test
    subparsers.add_parser("smoke-test", parents=[parent_parser], help="Execute minimal 2-request verification test")

    # seed
    subparsers.add_parser("seed", parents=[parent_parser], help="Pre-seed virtual users and cache JWTs")

    # run
    run_parser = subparsers.add_parser("run", parents=[parent_parser], help="Run a benchmark suite")
    run_parser.add_argument("--suite", required=True, choices=["obj1_bottleneck", "obj2_infinite_gpu", "obj3_vllm_sweep", "obj4_sla_curve", "custom"])
    run_parser.add_argument("--arrival-mode", default="stepped-poisson", choices=["stepped-poisson", "closed-loop", "burst"])
    run_parser.add_argument("--rates", default="2,4,8,12,16", help="Comma-separated rates/min for stepped-poisson")
    run_parser.add_argument("--concurrency", default="5,10,20,35,50", help="Comma-separated concurrent users for closed-loop")
    run_parser.add_argument("--bursts", default="10,20,30", help="Comma-separated burst sizes for burst mode")
    run_parser.add_argument("--step-duration-s", type=float, default=120.0, help="Duration of each step in seconds")
    run_parser.add_argument("--min-completions", type=int, default=10, help="Minimum completions per step")
    run_parser.add_argument("--burst-window-s", type=float, default=5.0, help="Window duration for burst mode")
    run_parser.add_argument("--cold-start", action="store_true", help="Flag if testing cold start from min replicas")

    # plot
    plot_parser = subparsers.add_parser("plot", parents=[parent_parser], help="Generate plots for an existing run")
    plot_parser.add_argument("--run-id", default="latest", help="Run ID to plot (or 'latest')")

    # export
    export_parser = subparsers.add_parser("export", parents=[parent_parser], help="Export CSV and JSON artifacts for a run")
    export_parser.add_argument("--run-id", default="latest", help="Run ID to export (or 'latest')")

    args = parser.parse_args()
    setup_logging(args.verbose)

    config_kwargs: dict[str, Any] = {
        "hardware_tag": args.hardware_tag,
        "mock_llm_enabled": args.mock_llm,
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
        config_kwargs["user_pool_size"] = args.users

    config = BenchmarkConfig(**config_kwargs)

    if args.command == "smoke-test":
        sys.exit(asyncio.run(_run_smoke_test(config)))

    elif args.command == "seed":
        seeder = Seeder(config)
        tokens = asyncio.run(seeder.ensure_users_and_tokens())
        console.print(f"[bold green]✓ Seeded {len(tokens)} virtual users and cached tokens![/bold green]")
        sys.exit(0)

    elif args.command == "run":
        sys.exit(asyncio.run(_run_benchmark(config, args)))

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
