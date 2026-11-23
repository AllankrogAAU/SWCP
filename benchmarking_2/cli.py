from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx

from .cluster_switch import (
    drain_cluster_pipeline,
    ensure_azure_worker_config,
    resolve_model_path,
    switch_local_model,
)
from .config import Config, ModelSpec, parse_models
from .dataset import CATEGORY_DEFINITIONS, DatasetStore, Submission, dataset_hash, load_submissions
from .judge import CloudJudge
from .pipeline import PipelineClient, Result
from .reports import generate_reports
from .storage import Store


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SWCP second-generation benchmark")
    parser.add_argument("mode", choices=("prepare", "complete", "latency", "test-test", "status", "judge"))
    parser.add_argument("--data", type=Path, default=Path("benchmarks/submissions_data_11000.jsonl"))
    parser.add_argument("--results", type=Path, default=Path("benchmarking_2/results"))
    parser.add_argument("--target-url", default=None)
    parser.add_argument("--models", default=None, help="Comma-separated backend:name values.")
    parser.add_argument("--sample-per-category", type=int, default=None)
    parser.add_argument("--rpm", type=float, default=None, help="Target submissions per minute (default: 100).")
    parser.add_argument("--concurrency", type=int, default=None, help="Max in-flight submissions (default: VLLM_MAX_NUM_SEQS=10).")
    parser.add_argument("--judge-batch-size", type=int, default=8)
    parser.add_argument("--users", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--switch-command", default=None, help="Command template containing {model}.")
    parser.add_argument("--run-id", default=None, help="Existing result run ID for judge or report commands.")
    parser.add_argument("--resume", default=None, help="Resume an existing run ID, reusing successful submissions.")
    daemon = parser.add_mutually_exclusive_group()
    daemon.add_argument(
        "--detach",
        action="store_true",
        help="Run in a separate session and keep it running when the terminal or VS Code closes.",
    )
    daemon.add_argument(
        "--foreground",
        action="store_true",
        help="Keep the benchmark attached to this terminal instead of detaching it.",
    )
    return parser.parse_args()


async def authenticate(config: Config) -> tuple[list[str], dict[str, dict]]:
    from benchmarks.seeder import Seeder
    seeder = Seeder(config)  # type: ignore[arg-type]
    tokens = await seeder.ensure_users_and_tokens(config.user_count)
    async with httpx.AsyncClient(base_url=config.target_url, timeout=30) as client:
        response = await client.get("/api/assignments", headers={"Authorization": f"Bearer {tokens[0]}"})
        response.raise_for_status()
        assignment_details = {
            str(item.get("title")): {
                "id": str(item["id"]),
                "description": str(item.get("description") or ""),
                "taskFocus": str(item.get("taskFocus") or ""),
            }
            for item in response.json()
        }
    return tokens, assignment_details


def switch_model(config: Config, command: str | None, model: ModelSpec) -> None:
    if model.backend == "azure":
        logging.getLogger(__name__).info("Ensuring Azure worker configuration for model %s.", model.name)
        drain_cluster_pipeline(timeout_s=45.0)
        ensure_azure_worker_config()
        return
    if command:
        rendered = command.format(model=model.name, backend=model.backend)
        logging.getLogger(__name__).info("Switching model with configured command: %s", rendered)
        subprocess.run(rendered, shell=True, check=True)
        return
    logging.getLogger(__name__).info(
        "Switching vLLM to local model %s (VLLM_MAX_NUM_SEQS=%d).",
        model.name,
        config.max_concurrency,
    )
    switch_local_model(model.name, config.models_dir, max_num_seqs=config.max_concurrency)


def validate_model_arguments(models: list[ModelSpec], models_dir: Path) -> None:
    if not models:
        raise ValueError("At least one model is required.")
    for model in models:
        if model.backend == "local":
            if "your" in model.name.lower() or "placeholder" in model.name.lower():
                raise ValueError(
                    f"Invalid placeholder local model '{model.name}'. "
                    "Use one of the model names printed by --help discovery or a real directory name."
                )
            resolve_model_path(model.name, models_dir)


def print_status(results_dir: Path) -> int:
    databases = sorted(glob.glob(str(results_dir / "*.sqlite")))
    if not databases:
        print("No benchmark run database found.")
        return 1
    latest: tuple[str, str, str, str | None, str] | None = None
    latest_database: str | None = None
    for database in databases:
        connection = sqlite3.connect(database)
        try:
            run_row = connection.execute(
                "SELECT run_id, mode, started_at, ended_at, config_json "
                "FROM runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        except sqlite3.DatabaseError:
            run_row = None
        finally:
            connection.close()
        if run_row is not None and (
            latest is None or str(run_row[2]) > latest[2]
        ):
            latest = (str(run_row[0]), str(run_row[1]), str(run_row[2]), run_row[3], str(run_row[4] or "{}"))
            latest_database = database
    if latest is None or latest_database is None:
        print("No benchmark run found.")
        return 1
    database = latest_database
    connection = sqlite3.connect(database)
    run_id, mode, started_at, ended_at, config_json_raw = latest
    try:
        run_cfg = json.loads(config_json_raw)
    except json.JSONDecodeError:
        run_cfg = {}
    print(f"Database: {database}")
    print(f"Run: {run_id}")
    print(f"Mode: {mode}")
    print(f"Started: {started_at}")
    print(f"Finished: {ended_at or 'running'}")
    try:
        active = subprocess.run(
            ["kubectl", "get", "configmap", "vllm-config", "-n", "swcp",
             "-o", "jsonpath={.data.LLM_MODEL_NAME}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        print(f"Active vLLM model: {active or 'unknown'}")
    except (OSError, subprocess.CalledProcessError):
        print("Active vLLM model: unavailable")
    print("\nProgress by model:")
    for row in connection.execute(
        """
        SELECT model_name, backend,
               SUM(warmup=0) AS measured,
               SUM(warmup=0 AND status='COMPLETED') AS completed,
               SUM(warmup=0 AND status='FAILED') AS failed,
               SUM(warmup=0 AND feedback IS NOT NULL) AS feedback_given,
               SUM(warmup=1) AS warmups
        FROM submissions
        WHERE run_id=?
        GROUP BY model_name, backend
        """,
        (run_id,),
    ):
        print(
            f"  {row[0]} [{row[1]}]: measured={row[2] or 0}, "
            f"completed={row[3] or 0}, failed={row[4] or 0}, "
            f"feedback={row[5] or 0}, warmups={row[6] or 0}"
        )
    print("\nCloud judge progress:")
    for row in connection.execute(
        "SELECT model_name, COUNT(*) FROM judge_results WHERE run_id=? "
        "GROUP BY model_name ORDER BY model_name",
        (run_id,),
    ):
        print(f"  {row[0]}: {row[1]} judgements")
    measured = connection.execute(
        "SELECT COUNT(*) FROM submissions WHERE run_id=? AND warmup=0",
        (run_id,),
    ).fetchone()[0]
    average_ms = connection.execute(
        "SELECT AVG(client_e2e_ms) FROM submissions "
        "WHERE run_id=? AND warmup=0 AND client_e2e_ms IS NOT NULL",
        (run_id,),
    ).fetchone()[0] or 0
    sample = connection.execute(
        "SELECT sample_per_category FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()[0]
    distinct_cats = connection.execute(
        "SELECT COUNT(DISTINCT category) FROM submissions WHERE run_id=? AND warmup=0",
        (run_id,),
    ).fetchone()[0]
    category_count = max(distinct_cats, len(CATEGORY_DEFINITIONS))
    cfg_models = run_cfg.get("models")
    configured_model_count = len(cfg_models) if isinstance(cfg_models, list) and cfg_models else 0
    observed_model_count = connection.execute(
        "SELECT COUNT(DISTINCT model_name) FROM submissions WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    model_count = max(configured_model_count, observed_model_count)
    expected = sample * category_count * model_count
    remaining = max(0, expected - measured)
    target_rpm = float(run_cfg.get("target_rpm") or 100.0)
    eta_s = (remaining / target_rpm) * 60.0 if target_rpm > 0 else (remaining * float(average_ms) / 1000 if average_ms else 0)
    print(f"Measured submissions: {measured}/{expected} ({category_count} categories x {sample} x {model_count} models)")
    print(f"Remaining submissions: {remaining}")
    print(f"Average E2E: {float(average_ms):.1f} ms")
    print(f"Estimated remaining time (@ {target_rpm:.0f} RPM): {time.strftime('%Hh %Mm %Ss', time.gmtime(eta_s))}")
    print("")
    connection.close()
    return 0


async def _judge_unjudged_rows(
    config: Config,
    store: Store,
    run_id: str,
    source_by_record: dict[int, Submission],
    model_name: str | None = None,
) -> int:
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        if model_name:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT s.* FROM submissions s
                    LEFT JOIN judge_results j
                      ON j.run_id = s.run_id AND j.model_name = s.model_name AND j.record_id = s.record_id
                    WHERE s.run_id = ? AND s.model_name = ? AND s.warmup = 0
                      AND s.status = 'COMPLETED' AND j.record_id IS NULL
                    ORDER BY s.record_id
                    """,
                    (run_id, model_name),
                ).fetchall()
            ]
        else:
            rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT s.* FROM submissions s
                    LEFT JOIN judge_results j
                      ON j.run_id = s.run_id AND j.model_name = s.model_name AND j.record_id = s.record_id
                    WHERE s.run_id = ? AND s.warmup = 0
                      AND s.status = 'COMPLETED' AND j.record_id IS NULL
                    ORDER BY s.model_name, s.record_id
                    """,
                    (run_id,),
                ).fetchall()
            ]

    if not rows:
        return 0

    dataset_store = DatasetStore(config.dataset_db_path)
    assignment_meta = dataset_store.load_assignment_details()
    for row in rows:
        source = source_by_record.get(int(row["record_id"]))
        if source is not None:
            row["student_code"] = source.source_code
            row["standard_input"] = source.standard_input
        meta = assignment_meta.get(str(row.get("assignment_title", "")), {})
        row["assignment_description"] = meta.get("description", "")
        row["teacher_focus"] = meta.get("task_focus", "")

    logging.info(
        "Cloud judging %d unjudged submissions%s...",
        len(rows),
        f" for {model_name}" if model_name else "",
    )

    judged_count = 0

    def _save_one(row_dict: dict, score_dict: dict) -> None:
        nonlocal judged_count
        store.judge(
            run_id,
            str(row_dict["model_name"]),
            int(row_dict["record_id"]),
            score_dict,
        )
        judged_count += 1

    await CloudJudge(config).judge_rows(rows, on_row_judged=_save_one)
    logging.info(
        "Completed judging %d submissions%s.",
        judged_count,
        f" for {model_name}" if model_name else "",
    )
    return judged_count


async def run(args: argparse.Namespace) -> Path:
    config = Config.default()
    config.data_path, config.results_dir = args.data, args.results
    if args.rpm is not None:
        config.target_rpm = args.rpm
    if args.concurrency is not None:
        config.max_concurrency = args.concurrency
    if args.mode == "status":
        print_status(config.results_dir)
        return config.results_dir
    if args.mode == "judge":
        databases = sorted(glob.glob(str(config.results_dir / "*.sqlite")))
        if not databases:
            raise RuntimeError("No benchmark result database found.")
        selected_database = None
        run_id = args.run_id
        latest: tuple[str, str] | None = None
        for database in databases:
            connection = sqlite3.connect(database)
            try:
                if run_id:
                    row = connection.execute(
                        "SELECT run_id, started_at FROM runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        "SELECT run_id, started_at FROM runs ORDER BY started_at DESC LIMIT 1"
                    ).fetchone()
            except sqlite3.DatabaseError:
                row = None
            finally:
                connection.close()
            if row and (latest is None or str(row[1]) > latest[1]):
                latest = (str(row[0]), str(row[1]))
                selected_database = database
        if latest is not None:
            run_id = latest[0]
        if selected_database is None or run_id is None:
            raise RuntimeError(f"Benchmark run not found: {args.run_id or 'latest'}")
        source_by_record = {
            item.record_id: item
            for item in load_submissions(config.data_path)
        }
        store = Store(Path(selected_database))
        judged_count = await _judge_unjudged_rows(config, store, run_id, source_by_record)
        export_dir = store.export(run_id)
        generate_reports(store.path, run_id, export_dir)
        logging.info("Judged %d unjudged submissions and wrote reports to %s", judged_count, export_dir)
        return export_dir

    dataset_store = DatasetStore(config.dataset_db_path)
    if args.mode == "prepare":
        source_hash = dataset_store.rebuild(config.data_path)
        logging.info(
            "Prepared dataset SQLite: %s (%d categories, source hash %s)",
            dataset_store.path,
            len(CATEGORY_DEFINITIONS),
            source_hash,
        )
        return dataset_store.path

    config.target_url = args.target_url or config.target_url
    config.models = parse_models(args.models) if args.models else config.models
    config.sample_per_category = args.sample_per_category or 50
    config.judge_batch_size = args.judge_batch_size
    config.user_count = max(args.users, config.max_concurrency * 2)
    config.timeout_s, config.seed = args.timeout, args.seed
    config.switch_command = args.switch_command or config.switch_command
    if args.mode == "test-test":
        if len(config.models) != 1 or config.models[0].backend != "local":
            raise ValueError("test-test requires exactly one local model, e.g. --models local:qwen.")
    if args.resume and not args.models:
        for candidate in glob.glob(str(config.results_dir / "*.sqlite")):
            with sqlite3.connect(candidate) as connection:
                row = connection.execute(
                    "SELECT config_json FROM runs WHERE run_id=?",
                    (args.resume,),
                ).fetchone()
            if row:
                resumed_config = json.loads(row[0])
                resumed_models = resumed_config.get("models")
                if isinstance(resumed_models, list):
                    config.models = [
                        ModelSpec(name=item["name"], backend=item["backend"])
                        for item in resumed_models
                        if isinstance(item, dict)
                    ]
                if not args.target_url:
                    config.target_url = resumed_config.get("target_url", config.target_url)
                break
    validate_model_arguments(config.models, config.models_dir)
    source_records = load_submissions(config.data_path)
    source_by_record = {item.record_id: item for item in source_records}
    source_hash = dataset_hash(source_records)
    dataset_store.rebuild(config.data_path)
    records = dataset_store.select(config.sample_per_category, config.seed)
    run_id = f"{args.mode}-{uuid.uuid4().hex[:10]}"
    store_path = config.results_dir / f"{run_id}.sqlite"
    if args.resume:
        resume_database = None
        for candidate in glob.glob(str(config.results_dir / "*.sqlite")):
            with sqlite3.connect(candidate) as connection:
                row = connection.execute(
                    "SELECT config_json FROM runs WHERE run_id=?",
                    (args.resume,),
                ).fetchone()
            if row:
                resume_database = candidate
                break
        if resume_database is None:
            raise RuntimeError(f"Run to resume was not found: {args.resume}")
        run_id = args.resume
        store_path = Path(resume_database)
        with sqlite3.connect(store_path) as connection:
            run_row = connection.execute(
                "SELECT config_json FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
        resumed_config = json.loads(run_row[0]) if run_row else {}
        resumed_models = resumed_config.get("models")
        if not args.models and isinstance(resumed_models, list):
            config.models = [
                ModelSpec(name=item["name"], backend=item["backend"])
                for item in resumed_models
                if isinstance(item, dict)
            ]
        if not args.target_url:
            config.target_url = resumed_config.get("target_url", config.target_url)

    store = Store(store_path)
    if not args.resume:
        store.start_run(run_id, args.mode, source_hash, config.sample_per_category, args.mode != "latency", asdict(config))
    logging.info(
        "Run %s: %d submissions/model (%d categories x %d) across %d models @ %.0f RPM (max_concurrency=%d, loki=%s)",
        run_id,
        len(records),
        len(CATEGORY_DEFINITIONS),
        config.sample_per_category,
        len(config.models),
        config.target_rpm,
        config.max_concurrency,
        config.loki_url or "disabled",
    )
    try:
        tokens, assignments = await authenticate(config)
        dataset_store.sync_api_assignments(assignments)
        for model_idx, model in enumerate(config.models, 1):
            logging.info("=== [%d/%d] Preparing model %s (%s) ===", model_idx, len(config.models), model.name, model.backend)
            existing_warmups: set[int] = set()
            existing_completed: set[int] = set()
            if args.resume:
                with sqlite3.connect(store.path) as connection:
                    existing_warmups = {
                        row[0]
                        for row in connection.execute(
                            "SELECT record_id FROM submissions "
                            "WHERE run_id=? AND model_name=? AND warmup=1 AND status='COMPLETED'",
                            (run_id, model.name),
                        )
                    }
                    existing_completed = {
                        row[0]
                        for row in connection.execute(
                            "SELECT record_id FROM submissions "
                            "WHERE run_id=? AND model_name=? AND warmup=0 AND status='COMPLETED'",
                            (run_id, model.name),
                        )
                    }
            pending_items = [
                item for item in records
                if not (args.resume and item.record_id in existing_completed)
            ]
            if pending_items:
                try:
                    switch_model(config, config.switch_command, model)
                except Exception as exc:
                    logging.error("Model switch failed for %s: %s. Skipping model.", model.name, exc)
                    continue
                client = PipelineClient(config, model, tokens, assignments)
                warmup_item = records[0]
                warmups_needed = max(0, config.warmup_count - len(existing_warmups))
                warmup_ok = True
                for warmup_index in range(warmups_needed):
                    warmup: Result | None = None
                    for attempt in range(1, 4):
                        warmup = await client.submit(warmup_item, warmup=True)
                        if warmup.status == "COMPLETED":
                            break
                        logging.warning(
                            "Warm-up %d/%d attempt %d/3 failed for %s (status=%s, error=%s); retrying...",
                            warmup_index + 1,
                            warmups_needed,
                            attempt,
                            model.name,
                            warmup.status,
                            warmup.error,
                        )
                        await asyncio.sleep(3.0)
                    if warmup is None or warmup.status != "COMPLETED":
                        logging.error(
                            "Warm-up failed permanently for %s: status=%s, error=%s. Skipping model.",
                            model.name,
                            warmup.status if warmup else "NONE",
                            warmup.error if warmup else "unknown",
                        )
                        warmup_ok = False
                        break
                    warmup_dict = result_dict(warmup, warmup=True)
                    warmup_dict["record_id"] = -(warmup_index + 1)
                    store.submission(run_id, model.name, model.backend, warmup_dict)
                if not warmup_ok:
                    continue

                def _store_measured(res: Result, _m: ModelSpec = model) -> None:
                    store.submission(run_id, _m.name, _m.backend, result_dict(res))

                await client.submit_batch(
                    pending_items,
                    target_rpm=config.target_rpm,
                    max_concurrency=config.max_concurrency,
                    on_complete=_store_measured,
                )
            else:
                logging.info("All %d submissions already completed for %s.", len(records), model.name)

            if args.mode != "latency":
                try:
                    await _judge_unjudged_rows(config, store, run_id, source_by_record, model_name=model.name)
                except Exception as exc:
                    logging.warning(
                        "Cloud judging interrupted for %s (%s: %s); will retry at end of run and continue to next model.",
                        model.name,
                        type(exc).__name__,
                        exc,
                    )

        if args.mode != "latency":
            try:
                await _judge_unjudged_rows(config, store, run_id, source_by_record, model_name=None)
            except Exception as exc:
                logging.warning(
                    "Final cloud judging pass encountered an error; exporting available results: %s",
                    exc,
                )
    finally:
        store.finish_run(run_id)
    artifact_dir = store.export(run_id)
    try:
        generate_reports(store.path, run_id, artifact_dir)
    except Exception as exc:
        logging.warning("Result report generation failed: %s", exc)
    logging.info("Artifacts written to %s (database: %s)", artifact_dir, store.path)
    return artifact_dir


def result_dict(result: Result, warmup: bool = False) -> dict:
    timings = result.timings
    return {
        "record_id": result.item.record_id, "assignment_title": result.item.assignment_title,
        "category": result.item.category, "normalized_category": result.item.normalized_category,
        "submission_id": result.submission_id, "status": result.status, "feedback": result.feedback,
        "sandbox_output": result.sandbox_output, "error": result.error,
        "client_e2e_ms": result.client_e2e_ms, "http_accepted_ms": result.http_accepted_ms,
        "pipeline_total_ms": getattr(timings, "pipeline_total_ms", 0),
        "sandbox_queue_ms": getattr(timings, "sandbox_queue_ms", 0),
        "sandbox_exec_ms": getattr(timings, "sandbox_exec_ms", 0),
        "llm_queue_ms": getattr(timings, "llm_queue_ms", 0),
        "llm_exec_ms": getattr(timings, "llm_exec_ms", 0),
        "orchestration_ms": getattr(timings, "orchestration_ms", 0),
        "delivery_ms": max(0.0, result.client_e2e_ms - getattr(timings, "pipeline_total_ms", 0)),
        "confidence_score_percent": result.confidence_score_percent,
        "sandbox_acquire_ms": getattr(timings, "sandbox_acquire_ms", 0),
        "sandbox_acquire_source": getattr(timings, "sandbox_acquire_source", "unknown"),
        "sandbox_compile_ms": getattr(timings, "sandbox_compile_ms", 0),
        "sandbox_run_ms": getattr(timings, "sandbox_run_ms", 0),
        "sandbox_backend": getattr(timings, "sandbox_backend", ""),
        "sandbox_runtime": getattr(timings, "sandbox_runtime", ""),
        "sandbox_worker_node": getattr(timings, "sandbox_worker_node", ""),
        "sandbox_worker_pod": getattr(timings, "sandbox_worker_pod", ""),
        "warmup": warmup,
    }


def launch_detached(command: list[str], pid_path: Path, log_path: Path) -> int:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with log_path.open("ab", buffering=0) as log:
        process = subprocess.Popen(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid


def detach_command(args: argparse.Namespace) -> int:
    argv = [
        argument
        for argument in sys.argv[1:]
        if argument not in {"--detach", "--foreground"}
    ]
    benchmark_dir = Path(__file__).resolve().parent
    pid_path = benchmark_dir / f"{args.mode}.pid"
    log_path = benchmark_dir / f"{args.mode}.log"
    command = [sys.executable, "-m", "benchmarking_2", *argv, "--foreground"]
    pid = launch_detached(command, pid_path, log_path)
    print(f"Benchmark started detached (PID {pid}).")
    print(f"Log: {log_path}")
    print(f"PID file: {pid_path}")
    print("Check progress with: python3 -m benchmarking_2 status")
    return pid


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = arguments()
    if args.mode in {"complete", "latency", "test-test", "judge"} and not args.foreground:
        detach_command(args)
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
