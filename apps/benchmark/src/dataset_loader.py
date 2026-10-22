"""Deterministic Project CodeNet sampling and a separate repository validation slice.

Each returned record (and CLI JSONL line) has these fields:
``id``, ``slice`` ("codenet" or "repo_validation"), ``problem_id``,
``submission_id``, ``status`` (CE/RE/WA/TLE_MLE/AC), ``original_status``,
``source_path``, ``source``, ``source_lines``, ``problem_description``,
``reference_submission_id``, and ``reference_source``.
Repository fixtures have null problem/submission/status/description/reference
fields: they are validation inputs, not inferred CodeNet verdicts, and are not
subject to CodeNet's length or reference requirements. AC records have no
reference; every non-AC CodeNet record has an Accepted C reference.

The archive is streamed three times: metadata/descriptions, bounded source-line
validation, then selected source/reference bodies only. Nothing is extracted.
Nonselected source bodies are never retained. Selection is lexicographic by
problem/submission ID, independent of tar member order, with no random seed.
Archives may have a single enclosing directory such as Project_CodeNet/.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import tarfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Iterator


STATUS_BUCKETS = ("CE", "RE", "WA", "TLE_MLE", "AC")
STATUS_MAP = {
    "Compile Error": "CE",
    "Runtime Error": "RE",
    "Wrong Answer": "WA",
    "Time Limit Exceeded": "TLE_MLE",
    "Memory Limit Exceeded": "TLE_MLE",
    "Accepted": "AC",
}
DEFAULT_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "cSandbox" / "tests" / "fixtures" / "source_code"
)


class _StreamingReader(io.BufferedReader):
    def seekable(self) -> bool:
        # Tar's ExFileObject delegates this to _Stream, which lacks seekable().
        return False


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        elif not self.hidden and tag in {
            "p", "div", "br", "hr", "li", "h1", "h2", "h3", "pre",
            "section", "table", "tr", "td", "th",
        }:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag not in {"b", "i", "em", "strong", "span", "code"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def extract_description(html: str) -> str:
    """Return whitespace-normalized HTML text, excluding head/script/style."""
    parser = _DescriptionParser()
    parser.feed(html)
    parser.close()
    return " ".join("".join(parser.parts).split())


@dataclass(frozen=True)
class _Submission:
    problem_id: str
    submission_id: str
    original_status: str
    status: str

    @property
    def path(self) -> str:
        return f"data/{self.problem_id}/C/{self.submission_id}.c"


def _archive_path(name: str) -> str:
    parts = PurePosixPath(name).parts
    if not parts or ".." in parts:
        return ""
    roots = {"metadata", "data", "problem_descriptions"}
    if parts[0] not in roots:
        parts = parts[1:]
    return "/".join(parts) if parts and parts[0] in roots else ""


def _valid_id(value: str) -> bool:
    return bool(value) and value not in {".", ".."} and "/" not in value and "\\" not in value


def _line_count(stream: io.TextIOBase, maximum: int) -> int:
    """Count universal-newline source lines with bounded buffers and early exit."""
    count = 0
    last = ""
    while chunk := stream.read(65536):
        count += chunk.count("\n")
        last = chunk[-1]
        if count > maximum:
            return count
    return count + int(bool(last) and last != "\n")


def load_codenet(
    archive_path: str | Path,
    samples_per_status: int = 20,
    *,
    min_lines: int = 15,
    max_lines: int = 120,
) -> list[dict[str, object]]:
    """Load up to N eligible C samples per bucket using the module record schema.

    Undersupplied buckets return fewer samples, never invalid replacements.
    References also satisfy the C source/line constraints. Malformed or missing
    metadata rows, source members, and descriptions are skipped.
    """
    if samples_per_status < 0:
        raise ValueError("samples_per_status must be nonnegative")
    if min_lines < 1 or max_lines < min_lines:
        raise ValueError("require 1 <= min_lines <= max_lines")
    if samples_per_status == 0:
        return []

    submissions: dict[str, _Submission] = {}
    descriptions: dict[str, str] = {}
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for member in archive:
            if not member.isfile():
                continue
            path = _archive_path(member.name)
            parts = path.split("/")
            if len(parts) != 2:
                continue
            if parts[0] == "metadata" and parts[1].endswith(".csv"):
                problem = parts[1][:-4]
                raw = archive.extractfile(member)
                if raw is None:
                    continue
                with io.TextIOWrapper(_StreamingReader(raw), encoding="utf-8-sig", errors="replace") as stream:
                    for row in csv.DictReader(stream):
                        pid = row.get("problem_id") or ""
                        sid = row.get("submission_id") or ""
                        original = row.get("status") or ""
                        if (
                            pid == problem and _valid_id(pid) and _valid_id(sid)
                            and row.get("language") == "C"
                            and row.get("filename_ext") in {"c", ".c"}
                            and original in STATUS_MAP
                        ):
                            submission = _Submission(pid, sid, original, STATUS_MAP[original])
                            submissions[submission.path] = submission
            elif parts[0] == "problem_descriptions" and parts[1].endswith(".html"):
                raw = archive.extractfile(member)
                if raw is not None:
                    with raw:
                        text = extract_description(raw.read().decode("utf-8", errors="replace"))
                    if text:
                        descriptions[parts[1][:-5]] = text

    eligible: dict[str, int] = {}
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for member in archive:
            path = _archive_path(member.name)
            submission = submissions.get(path)
            if not member.isfile() or submission is None or submission.problem_id not in descriptions:
                continue
            raw = archive.extractfile(member)
            if raw is None:
                continue
            with io.TextIOWrapper(_StreamingReader(raw), encoding="utf-8", errors="replace") as stream:
                lines = _line_count(stream, max_lines)
            if min_lines <= lines <= max_lines:
                eligible[path] = lines

    ordered = sorted(
        (submissions[path] for path in eligible),
        key=lambda submission: (submission.problem_id, submission.submission_id),
    )
    references: dict[str, _Submission] = {}
    for submission in ordered:
        if submission.status == "AC":
            references.setdefault(submission.problem_id, submission)
    selected: list[_Submission] = []
    counts = dict.fromkeys(STATUS_BUCKETS, 0)
    for submission in ordered:
        if counts[submission.status] >= samples_per_status:
            continue
        if submission.status != "AC" and submission.problem_id not in references:
            continue
        selected.append(submission)
        counts[submission.status] += 1

    needed = {submission.path for submission in selected}
    needed.update(
        references[submission.problem_id].path
        for submission in selected if submission.status != "AC"
    )
    bodies: dict[str, str] = {}
    if needed:
        with tarfile.open(archive_path, mode="r|gz") as archive:
            for member in archive:
                path = _archive_path(member.name)
                if member.isfile() and path in needed:
                    raw = archive.extractfile(member)
                    if raw is not None:
                        with io.TextIOWrapper(_StreamingReader(raw), encoding="utf-8", errors="replace") as stream:
                            bodies[path] = stream.read()
                if len(bodies) == len(needed):
                    break

    records: list[dict[str, object]] = []
    for submission in selected:
        reference = references.get(submission.problem_id) if submission.status != "AC" else None
        records.append({
            "id": f"codenet:{submission.problem_id}:{submission.submission_id}",
            "slice": "codenet",
            "problem_id": submission.problem_id,
            "submission_id": submission.submission_id,
            "status": submission.status,
            "original_status": submission.original_status,
            "source_path": submission.path,
            "source": bodies[submission.path],
            "source_lines": eligible[submission.path],
            "problem_description": descriptions[submission.problem_id],
            "reference_submission_id": reference.submission_id if reference else None,
            "reference_source": bodies[reference.path] if reference else None,
        })
    return records


def load_repo_fixtures(fixture_dir: str | Path = DEFAULT_FIXTURE_DIR) -> Iterator[dict[str, object]]:
    """Yield sorted *.c files as the separate, unlabelled validation slice.

    A missing fixture directory raises FileNotFoundError rather than silently
    dropping validation coverage. These files are read only, never executed.
    """
    directory = Path(fixture_dir)
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    for path in sorted(directory.glob("*.c")):
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        yield {
            "id": f"repo_validation:{path.name}",
            "slice": "repo_validation",
            "problem_id": None,
            "submission_id": None,
            "status": None,
            "original_status": None,
            "source_path": path.as_posix(),
            "source": source,
            "source_lines": len(source.splitlines()),
            "problem_description": None,
            "reference_submission_id": None,
            "reference_source": None,
        }


def main(argv: list[str] | None = None) -> int:
    """Write CodeNet records followed by repository validation records as JSONL.

    Usage: python dataset_loader.py ARCHIVE.tar.gz --samples-per-status 20
    Output defaults to stdout; --output PATH writes a UTF-8 JSONL file.
    --fixtures-dir overrides the repository fixture directory.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--samples-per-status", type=int, default=20)
    parser.add_argument("--fixtures-dir", type=Path, default=DEFAULT_FIXTURE_DIR)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.samples_per_status < 0:
        parser.error("--samples-per-status must be nonnegative")
    records = load_codenet(args.archive, args.samples_per_status)
    records.extend(load_repo_fixtures(args.fixtures_dir))
    if args.output:
        with args.output.open("w", encoding="utf-8", newline="\n") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        for record in records:
            print(json.dumps(record, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
