"""Export a human-readable review of model feedback next to the LLM judge's scores.

Usage (from the repository root):
    python -m apps.benchmark.inspect_judgements <run_dir> [--model KEY] [--status AC] \
        [--per-model 3] [--sort low|high|random] [--output FILE]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

SOCRATIC_KEYS = ("socratic_elicitation", "socratic_scaffolding", "socratic_tone")
ACTIONABILITY_KEYS = ("jargon_suitability", "next_step_clarity")
QUESTION_PATTERN = re.compile(r"[^.!?\n]*\?")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def extract_questions(feedback: str) -> list[str]:
    without_code = re.sub(r"```.*?```", " ", feedback, flags=re.DOTALL)
    return [match.strip(" -*\t") for match in QUESTION_PATTERN.findall(without_code) if len(match.strip()) > 3]


def join_records(run_dir: Path) -> list[dict[str, Any]]:
    submissions = {
        row["submission_id"]: row
        for row in load_jsonl(run_dir / "results.jsonl")
        if row.get("record_type") == "submission" and row.get("submission_id")
    }
    joined = []
    for judgement in load_jsonl(run_dir / "judgements.jsonl"):
        if judgement.get("judge_status") != "ok":
            continue
        submission = submissions.get(judgement["submission_id"], {})
        feedback = submission.get("llm_feedback") or ""
        joined.append({
            "model": judgement.get("model_key"),
            "status": judgement.get("codenet_status"),
            "problem": judgement.get("problem_id"),
            "codenet_submission": judgement.get("codenet_submission_id"),
            "feedback": feedback,
            "questions": extract_questions(feedback),
            "raw": judgement.get("judge_raw") or {},
            "metrics": judgement.get("metrics") or {},
        })
    return joined


def socratic_mean(record: dict[str, Any]) -> float:
    values = [record["metrics"].get(key) for key in SOCRATIC_KEYS]
    values = [value for value in values if isinstance(value, (int, float))]
    return statistics.fmean(values) if values else 0.0


def _mean(records: list[dict[str, Any]], key: str) -> str:
    values = [r["metrics"].get(key) for r in records if isinstance(r["metrics"].get(key), (int, float))]
    return f"{statistics.fmean(values):.2f}" if values else "–"


def summary_table(by_model: dict[str, list[dict[str, Any]]]) -> list[str]:
    keys = ("socratic_guidance_score", *SOCRATIC_KEYS, *ACTIONABILITY_KEYS)
    lines = [
        "| Model | n | asks ≥1 question | " + " | ".join(keys) + " |",
        "|---|---|---|" + "---|" * len(keys),
    ]
    for model, records in sorted(by_model.items()):
        asked = sum(bool(r["questions"]) for r in records) / len(records) * 100
        lines.append(
            f"| {model} | {len(records)} | {asked:.0f}% | "
            + " | ".join(_mean(records, key) for key in keys)
            + " |"
        )
    return lines


def render_example(index: int, record: dict[str, Any]) -> list[str]:
    raw, metrics = record["raw"], record["metrics"]
    socratic = raw.get("socratic_subscores") or {}
    actionability = raw.get("actionability_subscores") or {}
    questions = record["questions"] or ["(no question asked)"]
    lines = [
        f"#### {index}. `{record['status']}` · problem {record['problem']} · "
        f"CodeNet {record['codenet_submission']}",
        "",
        "**Questions the model asked:**",
        "",
        *[f"- {question}" for question in questions],
        "",
        "**Judge scores:** "
        f"Socratic overall {raw.get('socratic_guidance_score', '–')}/5 "
        f"(elicitation {socratic.get('socratic_elicitation', '–')}, "
        f"scaffolding {socratic.get('socratic_scaffolding', '–')}, "
        f"tone {socratic.get('socratic_tone', '–')}) · "
        f"Actionability {raw.get('actionability_clarity_score', '–')}/5 "
        f"(jargon {actionability.get('jargon_suitability', '–')}, "
        f"next step {actionability.get('next_step_clarity', '–')}) · "
        f"Checklist {metrics.get('checklist_points', '–')}/5",
    ]
    if raw.get("reports_nonexistent_bug"):
        categories = ", ".join(raw.get("hallucinated_issue_categories") or []) or "unspecified"
        lines.append(f"**Hallucinated bug:** {categories}")
    lines += [
        "",
        f"**Judge rationale:** {raw.get('rationale', '(none)')}",
        "",
        "<details><summary>Full model feedback</summary>",
        "",
        record["feedback"].strip() or "(empty)",
        "",
        "</details>",
        "",
    ]
    return lines


def build_report(records: list[dict[str, Any]], per_model: int, sort: str, seed: int) -> str:
    by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_model[record["model"]].append(record)

    lines = ["# Judge review: model questions and Socratic scoring", "", *summary_table(by_model), ""]
    rng = random.Random(seed)
    for model, model_records in sorted(by_model.items()):
        if sort == "random":
            chosen = rng.sample(model_records, min(per_model, len(model_records)))
        else:
            chosen = sorted(model_records, key=socratic_mean, reverse=sort == "high")[:per_model]
        lines += [f"## {model}", ""]
        for index, record in enumerate(chosen, 1):
            lines += render_example(index, record)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--model", action="append", help="Only include this model key (repeatable).")
    parser.add_argument("--status", action="append", choices=["AC", "CE", "RE", "WA"])
    parser.add_argument("--per-model", type=int, default=3)
    parser.add_argument("--sort", choices=["low", "high", "random"], default="random")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    records = join_records(args.run_dir)
    if args.model:
        records = [r for r in records if r["model"] in args.model]
    if args.status:
        records = [r for r in records if r["status"] in args.status]
    if not records:
        raise SystemExit("No judged records matched the filters.")

    output = args.output or args.run_dir / f"judge_review_{args.sort}.md"
    output.write_text(build_report(records, args.per_model, args.sort, args.seed), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
