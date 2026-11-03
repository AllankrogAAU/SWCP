"""Selective Project CodeNet archive extraction and C submission sampling."""

from __future__ import annotations

import csv
import html
from html.parser import HTMLParser
from itertools import chain
import random
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

PROBLEM_ID = re.compile(r"p\d{5}\Z")
SUBMISSION_ID = re.compile(r"s\d{9}\Z")
DEFAULT_STATUSES = ("AC", "CE", "RE", "WA", "TLE", "MLE")
STATUS_MAP = {
    "accepted": "AC",
    "compile error": "CE",
    "compilation error": "CE",
    "runtime error": "RE",
    "wrong answer": "WA",
    "time limit exceeded": "TLE",
    "memory limit exceeded": "MLE",
}
METADATA_COLUMNS = (
    "submission_id",
    "problem_id",
    "user_id",
    "date",
    "original_language",
    "language",
    "ext",
    "status",
    "cpu_time",
    "memory",
    "code_size",
    "accuracy",
)
BLOCK_TAGS = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt", "h1", "h2",
    "h3", "h4", "h5", "h6", "hr", "li", "ol", "p", "pre", "section", "table",
    "td", "th", "tr", "ul",
}
SKIP_TAGS = {"script", "style", "noscript"}


@dataclass(frozen=True)
class CodeNetSample:
    problem_id: str
    title: str
    status: str
    submission_id: str
    source_path: Path
    accepted_submission_id: str
    accepted_source_path: Path
    description: str
    sample_input: str
    expected_stdout: str
    timeout_ms: int
    memory_limit_mb: int

    def read_source(self) -> str:
        return self.source_path.read_text(encoding="utf-8")

    def read_accepted_source(self) -> str:
        return self.accepted_source_path.read_text(encoding="utf-8")

    def test_case(self) -> dict[str, int | str]:
        return {
            "stdin": self.sample_input,
            "expectedStdout": self.expected_stdout,
            "timeoutMilliseconds": self.timeout_ms,
            "memoryLimitMegabytes": self.memory_limit_mb,
        }


@dataclass(frozen=True)
class ProblemInfo:
    title: str
    time_limit_ms: int
    memory_limit_kb: int


@dataclass(frozen=True)
class _Candidate:
    problem_id: str
    status: str
    submission_id: str
    source_path: Path


class _DescriptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.parts.append(data)


def html_to_text(value: str, max_chars: int = 8_000) -> str:
    parser = _DescriptionParser()
    parser.feed(value)
    text = html.unescape(" ".join(" ".join(parser.parts).split()))
    return text[:max_chars].strip()


def normalize_status(value: str) -> str | None:
    return STATUS_MAP.get(" ".join(value.strip().lower().split()))


def _read_problem_list(path: Path) -> dict[str, ProblemInfo]:
    problems: dict[str, ProblemInfo] = {}
    with path.open(newline="", encoding="utf-8") as file:
        rows = csv.reader(file)
        first = next(rows, None)
        if first is None:
            return problems
        header = [part.strip().lower() for part in first]
        has_header = bool(header) and header[0] in {"id", "problem_id"}
        records: Iterable[list[str]]
        if has_header:
            records = rows
            indices = {name: index for index, name in enumerate(header)}
        else:
            records = chain((first,), rows)
            indices = {
                "id": 0,
                "name": 1,
                "time_limit": 3,
                "memory_limit": 4,
            }

        for row in records:
            if len(row) <= max(indices.get("id", 0), indices.get("name", 1)):
                continue
            problem_id = row[indices["id"]].strip()
            if not PROBLEM_ID.fullmatch(problem_id):
                continue

            def int_field(name: str) -> int:
                index = indices.get(name)
                if index is None or index >= len(row):
                    return 0
                try:
                    return int(row[index])
                except ValueError:
                    return 0

            problems[problem_id] = ProblemInfo(
                title=row[indices["name"]].strip(),
                time_limit_ms=int_field("time_limit"),
                memory_limit_kb=int_field("memory_limit"),
            )
    return problems


def _read_problem_submissions(path: Path, problem_id: str) -> Iterable[tuple[str, str, str]]:
    with path.open(newline="", encoding="utf-8") as file:
        rows = csv.reader(file)
        first = next(rows, None)
        if first is None:
            return
        header = [part.strip().lower() for part in first]
        has_header = bool(header) and header[0] in {"submission_id", "id"}
        if has_header:
            indices = {name: index for index, name in enumerate(header)}
            records = rows
        else:
            indices = {name: index for index, name in enumerate(METADATA_COLUMNS)}
            records = chain((first,), rows)
        extension_column = "ext" if "ext" in indices else "filename_ext"
        if extension_column not in indices:
            raise ValueError(f"Submission metadata is missing an ext or filename_ext column: {path}")

        for row in records:
            try:
                submission_id = row[indices["submission_id"]].strip()
                language = row[indices["language"]].strip()
                extension = row[indices[extension_column]].strip().lower().lstrip(".")
                status = row[indices["status"]].strip()
            except (IndexError, KeyError):
                continue
            if (
                not SUBMISSION_ID.fullmatch(submission_id)
                or language != "C"
                or extension != "c"
            ):
                continue
            yield submission_id, status, extension


def _read_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"p\d{5}", text))


def _safe_archive_member(member: tarfile.TarInfo, prefix: str) -> str | None:
    name = member.name
    if not member.isfile() or not name.startswith(prefix):
        return None
    relative = name[len(prefix):]
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return relative


def _extract_selected_metadata(archive_path: Path, cache_dir: Path) -> None:
    marker = cache_dir / ".metadata-ready"
    if marker.is_file():
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    allow = (
        re.compile(r"metadata/(?:problem_list|p\d{5})\.csv\Z"),
        re.compile(r"problem_descriptions/p\d{5}\.html\Z"),
        re.compile(r"derived/input_output/(?:unverified_accepted_solutions|no_solutions)\.txt\Z"),
        re.compile(r"derived/input_output/data/p\d{5}/(?:input|output)\.txt\Z"),
    )
    prefix = "Project_CodeNet/"
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for member in archive:
            relative = _safe_archive_member(member, prefix)
            if relative is None or not any(pattern.fullmatch(relative) for pattern in allow):
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            destination = cache_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    if not (cache_dir / "metadata/problem_list.csv").is_file():
        raise FileNotFoundError("CodeNet archive does not contain metadata/problem_list.csv.")
    marker.write_text("ready\n", encoding="ascii")


def _extract_sources(archive_path: Path, cache_dir: Path, source_names: set[str]) -> None:
    missing = {
        name for name in source_names
        if not (cache_dir / name.removeprefix("Project_CodeNet/")).is_file()
    }
    if not missing:
        return
    prefix = "Project_CodeNet/"
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for member in archive:
            if member.name not in missing or not member.isfile():
                continue
            relative = _safe_archive_member(member, prefix)
            if relative is None:
                continue
            source = archive.extractfile(member)
            if source is None:
                continue
            destination = cache_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            missing.remove(member.name)
            if not missing:
                break
    if missing:
        first_missing = sorted(missing)[0]
        raise FileNotFoundError(f"CodeNet source member is missing from the archive: {first_missing}")


def select_samples(
    dataset_root: Path,
    per_status: int = 100,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    seed: int = 2026,
    require_sources: bool = True,
    candidate_pool_multiplier: int = 1,
) -> list[CodeNetSample]:
    root = dataset_root.resolve()
    metadata_dir = root / "metadata"
    problem_list_path = metadata_dir / "problem_list.csv"
    if not problem_list_path.is_file():
        raise FileNotFoundError(f"CodeNet problem list not found: {problem_list_path}")
    if per_status <= 0:
        raise ValueError("per_status must be greater than zero.")
    if candidate_pool_multiplier <= 0:
        raise ValueError("candidate_pool_multiplier must be greater than zero.")
    requested = tuple(dict.fromkeys(status.strip().upper() for status in statuses))
    if not requested:
        raise ValueError("At least one CodeNet status must be requested.")
    unsupported = set(requested) - set(DEFAULT_STATUSES)
    if unsupported:
        raise ValueError(f"Unsupported CodeNet status categories: {', '.join(sorted(unsupported))}")

    problems = _read_problem_list(problem_list_path)
    unverified = _read_ids(root / "derived/input_output/unverified_accepted_solutions.txt")
    no_solutions = _read_ids(root / "derived/input_output/no_solutions.txt")
    rng = random.Random(seed)
    candidates: dict[str, list[tuple[_Candidate, _Candidate, ProblemInfo, str, str, str]]] = {
        status: [] for status in requested
    }

    for metadata_path in sorted(metadata_dir.glob("p?????.csv")):
        problem_id = metadata_path.stem
        if not PROBLEM_ID.fullmatch(problem_id) or problem_id in unverified or problem_id in no_solutions:
            continue
        problem = problems.get(problem_id)
        input_path = root / "derived/input_output/data" / problem_id / "input.txt"
        output_path = root / "derived/input_output/data" / problem_id / "output.txt"
        description_path = root / "problem_descriptions" / f"{problem_id}.html"
        if not problem or not input_path.is_file() or not output_path.is_file() or not description_path.is_file():
            continue
        sample_input = input_path.read_text(encoding="utf-8", errors="strict")
        expected_stdout = output_path.read_text(encoding="utf-8", errors="strict")
        if len(sample_input.encode("utf-8")) > 64 * 1024 or len(expected_stdout.encode("utf-8")) > 64 * 1024:
            continue
        description = html_to_text(description_path.read_text(encoding="utf-8", errors="replace"))
        if not description:
            continue

        accepted: _Candidate | None = None
        accepted_count = 0
        by_status: dict[str, tuple[list[_Candidate], int]] = {}
        for submission_id, raw_status, _ in _read_problem_submissions(metadata_path, problem_id):
            normalized = normalize_status(raw_status)
            if normalized == "AC":
                accepted_count += 1
                if rng.randrange(accepted_count) == 0:
                    accepted = _Candidate(
                        problem_id,
                        "AC",
                        submission_id,
                        root / "data" / problem_id / "C" / f"{submission_id}.c",
                    )
            elif normalized in requested:
                candidates_for_problem, count = by_status.get(normalized, ([], 0))
                count += 1
                candidate = _Candidate(
                    problem_id,
                    normalized,
                    submission_id,
                    root / "data" / problem_id / "C" / f"{submission_id}.c",
                )
                if len(candidates_for_problem) < candidate_pool_multiplier:
                    candidates_for_problem.append(candidate)
                else:
                    replacement = rng.randrange(count)
                    if replacement < candidate_pool_multiplier:
                        candidates_for_problem[replacement] = candidate
                by_status[normalized] = (candidates_for_problem, count)

        if accepted is None:
            continue
        for status in requested:
            if status == "AC":
                problem_candidates = [accepted]
            else:
                problem_candidates = by_status.get(status, ([], 0))[0]
                if not problem_candidates:
                    continue
            for candidate in problem_candidates:
                if require_sources and (
                    not candidate.source_path.is_file() or not accepted.source_path.is_file()
                ):
                    continue
                candidates[status].append(
                    (candidate, accepted, problem, description, sample_input, expected_stdout)
                )

    selected: list[CodeNetSample] = []
    for status in requested:
        pool = candidates[status]
        required_candidates = per_status * candidate_pool_multiplier
        if len(pool) < per_status:
            raise ValueError(
                f"Only {len(pool)} verified C problem pairs are available for {status}; "
                f"{per_status} are needed for the requested final samples."
            )
        for candidate, accepted, problem, description, sample_input, expected_stdout in rng.sample(
            pool, min(required_candidates, len(pool))
        ):
            timeout_ms = max(250, min(problem.time_limit_ms or 2_000, 3_000))
            memory_limit_mb = max(64, min((problem.memory_limit_kb + 1023) // 1024 or 128, 512))
            selected.append(
                CodeNetSample(
                    problem_id=candidate.problem_id,
                    title=problem.title,
                    status=status,
                    submission_id=candidate.submission_id,
                    source_path=candidate.source_path,
                    accepted_submission_id=accepted.submission_id,
                    accepted_source_path=accepted.source_path,
                    description=description,
                    sample_input=sample_input,
                    expected_stdout=expected_stdout,
                    timeout_ms=timeout_ms,
                    memory_limit_mb=memory_limit_mb,
                )
            )
    return selected


def load_archive_samples(
    archive_path: Path,
    cache_dir: Path,
    per_status: int = 100,
    statuses: tuple[str, ...] = DEFAULT_STATUSES,
    seed: int = 2026,
    candidate_pool_multiplier: int = 1,
) -> list[CodeNetSample]:
    archive = archive_path.expanduser().resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"CodeNet archive not found: {archive}")
    cache = cache_dir.expanduser().resolve()
    _extract_selected_metadata(archive, cache)
    samples = select_samples(
        cache, per_status, statuses, seed,
        require_sources=False,
        candidate_pool_multiplier=candidate_pool_multiplier,
    )
    source_names = {
        f"Project_CodeNet/data/{sample.problem_id}/C/{sample.submission_id}.c"
        for sample in samples
    } | {
        f"Project_CodeNet/data/{sample.problem_id}/C/{sample.accepted_submission_id}.c"
        for sample in samples
    }
    _extract_sources(archive, cache, source_names)
    return select_samples(
        cache, per_status, statuses, seed,
        require_sources=True,
        candidate_pool_multiplier=candidate_pool_multiplier,
    )
