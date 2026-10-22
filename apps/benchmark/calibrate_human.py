"""Blind human scoring of judged responses to compute human-vs-judge Cohen's kappa.

Usage: .venv-benchmark\\Scripts\\python.exe apps\\benchmark\\calibrate_human.py [--count 15]
Model names and judge scores are hidden while you score. Progress is saved after each
item to apps/benchmark/logs/human_scores.jsonl, so you can stop and resume.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from calibration_core import weighted_kappa  # noqa: E402
from judge_evaluator import DIMENSIONS, STATUS_NAMES, read_jsonl  # noqa: E402

LOG_DIR = Path(__file__).resolve().parent / "logs"


def ask_score(dimension: str) -> int:
    while True:
        answer = input(f"  {dimension} (0-5): ").strip()
        if answer.isdigit() and 0 <= int(answer) <= 5:
            return int(answer)
        print("  Enter an integer from 0 to 5.")


def select(judged: list[dict], count: int, seed: int) -> list[dict]:
    by_status: dict[str, list[dict]] = defaultdict(list)
    for row in judged:
        by_status[str(row.get("codenet_status"))].append(row)
    rng = random.Random(seed)
    for rows in by_status.values():
        rng.shuffle(rows)
    chosen: list[dict] = []
    while len(chosen) < count and any(by_status.values()):
        for status in sorted(by_status):
            if by_status[status] and len(chosen) < count:
                chosen.append(by_status[status].pop())
    rng.shuffle(chosen)
    return chosen


def report(scored: list[dict]) -> None:
    print(f"\nHuman vs. judge agreement over {len(scored)} items (quadratic-weighted Cohen's kappa):")
    for dimension in DIMENSIONS:
        kappa = weighted_kappa([r["human"][dimension] for r in scored], [r["judge"][dimension] for r in scored])
        print(f"  {dimension:34s} κw = {'n/a' if kappa is None else f'{kappa:.3f}'}")
    bucket = lambda score: 2 if score >= 70 else 1 if score >= 40 else 0  # noqa: E731
    human = [bucket(sum(r["human"].values()) / 25 * 100) for r in scored]
    judge = [bucket(r["judge_overall"]) for r in scored]
    kappa = weighted_kappa(human, judge, range(3))
    print(f"  {'overall (low/mid/high)':34s} κw = {'n/a' if kappa is None else f'{kappa:.3f}'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--judged", type=Path, default=LOG_DIR / "judged_logs.jsonl")
    parser.add_argument("--dataset", type=Path, default=LOG_DIR / "dataset.jsonl")
    parser.add_argument("--output", type=Path, default=LOG_DIR / "human_scores.jsonl")
    parser.add_argument("--count", type=int, default=15)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)

    judged = [r for r in read_jsonl(args.judged) if r.get("overall_score") is not None]
    if not judged:
        print("No judged responses yet. Run judge_evaluator.py first.", file=sys.stderr)
        return 1
    samples = {str(r["id"]): r for r in read_jsonl(args.dataset) if "id" in r}
    existing = {str(r["run_key"]): r for r in read_jsonl(args.output)}
    items = select(judged, args.count, args.seed)

    for number, row in enumerate(items, 1):
        if str(row["run_key"]) in existing:
            continue
        sample = samples.get(str(row.get("sample_id")), {})
        print("\n" + "=" * 100)
        print(f"Item {number}/{len(items)}   Official verdict: {STATUS_NAMES.get(str(row.get('codenet_status')), 'fixture')}")
        print("-" * 100 + "\nASSIGNMENT:\n" + (sample.get("problem_description") or "(none)")[:1500])
        print("-" * 100 + "\nSTUDENT CODE:\n" + (sample.get("source") or ""))
        print("-" * 100 + "\nFEEDBACK (model hidden):\n" + (row.get("feedback") or ""))
        print("-" * 100)
        human = {dimension: ask_score(dimension) for dimension in DIMENSIONS}
        record = {
            "run_key": row["run_key"], "sample_id": row.get("sample_id"), "model_name": row.get("model_name"),
            "human": human, "judge": {d: row[d] for d in DIMENSIONS}, "judge_overall": row["overall_score"],
        }
        with args.output.open("a", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(record) + "\n")
        existing[str(row["run_key"])] = record

    scored = [existing[str(r["run_key"])] for r in items if str(r["run_key"]) in existing]
    if scored:
        report(scored)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
