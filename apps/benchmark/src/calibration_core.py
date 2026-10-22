"""Compute Cohen's kappa (unweighted and quadratic-weighted) between two raters."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Sequence


def cohen_kappa(rows: list[dict[str, Any]], first: str, second: str) -> dict[str, Any]:
    pairs = [
        (str(row[first]), str(row[second]))
        for row in rows
        if row.get(first) not in (None, "") and row.get(second) not in (None, "")
    ]
    if not pairs:
        raise ValueError(f"No records contain both '{first}' and '{second}'.")
    first_counts = Counter(a for a, _ in pairs)
    second_counts = Counter(b for _, b in pairs)
    labels = sorted(set(first_counts) | set(second_counts))
    observed = sum(a == b for a, b in pairs) / len(pairs)
    expected = sum(first_counts[label] * second_counts[label] for label in labels) / (len(pairs) ** 2)
    kappa = (observed - expected) / (1 - expected) if expected != 1 else None
    confusion = {label: {other: sum(a == label and b == other for a, b in pairs) for other in labels} for label in labels}
    return {
        "n": len(pairs),
        "labels": labels,
        "observed_agreement": observed,
        "expected_agreement": expected,
        "cohen_kappa": kappa,
        "confusion_matrix": confusion,
    }


def weighted_kappa(first: Sequence[int], second: Sequence[int], categories: Sequence[int] = range(6)) -> float | None:
    """Quadratic-weighted Cohen's kappa for ordinal scores (e.g. 0-5)."""
    if len(first) != len(second) or not first:
        raise ValueError("Rater score lists must be non-empty and equal length.")
    cats = list(categories)
    k = len(cats)
    index = {value: position for position, value in enumerate(cats)}
    n = len(first)
    observed = [[0.0] * k for _ in range(k)]
    for a, b in zip(first, second):
        observed[index[a]][index[b]] += 1 / n
    row = [sum(observed[i]) for i in range(k)]
    col = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    weight = lambda i, j: ((i - j) ** 2) / ((k - 1) ** 2)  # noqa: E731
    disagreement_observed = sum(weight(i, j) * observed[i][j] for i in range(k) for j in range(k))
    disagreement_expected = sum(weight(i, j) * row[i] * col[j] for i in range(k) for j in range(k))
    return None if disagreement_expected == 0 else 1 - disagreement_observed / disagreement_expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSONL with two label fields.")
    parser.add_argument("--first", default="human_label")
    parser.add_argument("--second", default="judge_label")
    args = parser.parse_args(argv)
    try:
        rows = [json.loads(line) for line in args.input.read_text(encoding="utf-8").splitlines() if line.strip()]
        result = cohen_kappa(rows, args.first, args.second)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as error:
        print(f"Calibration failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
