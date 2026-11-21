from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path

import httpx

from .config import Config, ModelSpec, parse_models
from .dataset import DatasetStore, Submission, dataset_hash, load_submissions, select_submissions
from .judge import CloudJudge
from .pipeline import PipelineClient, Result
from .storage import Store


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SWCP second-generation benchmark")
    parser.add_argument("mode", choices=("prepare", "complete", "latency", "test-test"))
    parser.add_argument("--data", type=Path, default=Path("benchmarks/submissions_data_11000.jsonl"))
    parser.add_argument("--results", type=Path, default=Path("benchmarking_2/results"))
    parser.add_argument("--target-url", default=None)
    parser.add_argument("--models", default=None, help="Comma-separated backend:name values.")
    parser.add_argument("--sample-per-category", type=int, default=None)
    parser.add_argument("--judge-batch-size", type=int, default=8)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--switch-command", default=None, help="Command template containing {model}.")
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


def switch_model(command: str | None, model: ModelSpec) -> None:
    if not command:
        return
    rendered = command.format(model=model.name, backend=model.backend)
    logging.getLogger(__name__).info("Switching model with configured command: %s", rendered)
    subprocess.run(rendered, shell=True, check=True)


async def run(args: argparse.Namespace) -> Path:
    config = Config.default()
    config.data_path, config.results_dir = args.data, args.results
    dataset_store = DatasetStore(config.dataset_db_path)
    if args.mode == "prepare":
        source_hash = dataset_store.rebuild(config.data_path)
        logging.info("Prepared dataset SQLite: %s (source hash %s)", dataset_store.path, source_hash)
        return dataset_store.path
    config.target_url = args.target_url or config.target_url
    config.models = parse_models(args.models) if args.models else config.models
    config.sample_per_category = args.sample_per_category or (50 if args.mode == "test-test" else 100)
    config.judge_batch_size, config.user_count = args.judge_batch_size, args.users
    config.timeout_s, config.seed = args.timeout, args.seed
    config.switch_command = args.switch_command or config.switch_command
    if args.mode == "test-test":
        if len(config.models) != 1 or config.models[0].backend != "local":
            raise ValueError("test-test requires exactly one local model, e.g. --models local:qwen.")
    source_records = load_submissions(config.data_path)
    source_hash = dataset_hash(source_records)
    dataset_store.rebuild(config.data_path)
    records = dataset_store.select(config.sample_per_category, config.seed)
    run_id = f"{args.mode}-{uuid.uuid4().hex[:10]}"
    store = Store(config.results_dir / f"{run_id}.sqlite")
    store.start_run(run_id, args.mode, source_hash, config.sample_per_category, args.mode != "latency", asdict(config))
    logging.info("Run %s: %d submissions/category=%d models", run_id, len(records), len(config.models))
    try:
        tokens, assignments = await authenticate(config)
        dataset_store.sync_api_assignments(assignments)
        for model in config.models:
            switch_model(config.switch_command, model)
            client = PipelineClient(config, model, tokens, assignments)
            warmup_item = records[0]
            for warmup_index in range(config.warmup_count):
                warmup = await client.submit(warmup_item, warmup=True)
                if warmup.status != "COMPLETED":
                    raise RuntimeError(
                        f"Warm-up failed for {model.name}: status={warmup.status}, error={warmup.error}"
                    )
                warmup_dict = result_dict(warmup, warmup=True)
                warmup_dict["record_id"] = -(warmup_index + 1)
                store.submission(run_id, model.name, model.backend, warmup_dict)
            measured: list[Result] = []
            for item in records:
                result = await client.submit(item)
                measured.append(result)
                store.submission(run_id, model.name, model.backend, result_dict(result))
            if args.mode != "latency":
                scores = await CloudJudge(config).judge(measured)
                for result, score in zip(measured, scores):
                    store.judge(run_id, model.name, result.item.record_id, score)
    finally:
        store.finish_run(run_id)
    artifact_dir = store.export(run_id)
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
        "confidence_json": None,
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = arguments()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
