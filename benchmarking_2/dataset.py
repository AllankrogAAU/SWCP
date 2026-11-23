from __future__ import annotations

import collections
import hashlib
import json
import random
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path


CATEGORY_DEFINITIONS: dict[str, tuple[str, str]] = {
    "correct": (
        "Correct submissions",
        "Working solutions passing all tests, used to measure false positives and hallucinations.",
    ),
    "compile_syntax_error": (
        "Compile errors: syntax & delimiters",
        "Submissions that fail compilation due to missing semicolons, braces, parentheses, or quotes.",
    ),
    "compile_undeclared_error": (
        "Compile errors: undeclared identifier",
        "Submissions that fail compilation due to undeclared variable identifiers or omitted declarations.",
    ),
    "compile_type_error": (
        "Compile errors: invalid type name",
        "Submissions that fail compilation due to invalid or misspelled C type specifiers (e.g. integer, itn, Int, chr).",
    ),
    "infinite_loop_unconditional": (
        "Infinite loops: unconditional loop",
        "Submissions with an injected unconditional non-terminating loop (while(1), for(;;), do-while(1)).",
    ),
    "infinite_loop_condition_bug": (
        "Infinite loops: loop condition / step bug",
        "Submissions where a loop fails to terminate due to an omitted loop increment or wrong termination condition.",
    ),
    "null_pointer_deref": (
        "Pointer errors: null pointer dereference",
        "Submissions that dereference a null scalar or array pointer, causing a segmentation fault.",
    ),
    "null_struct_pointer_deref": (
        "Pointer errors: null struct member access",
        "Submissions that access a struct member through a null pointer (->), causing a segmentation fault.",
    ),
    "branch_condition_error": (
        "Logic errors: branch / relational condition",
        "Submissions with inverted or off-by-one relational/boolean operators in if statements or conditions.",
    ),
    "early_return_error": (
        "Logic errors: premature return / exit code",
        "Submissions with an injected early return before computation/output or a non-zero exit code from main.",
    ),
    "wrong_constant_or_init_error": (
        "Logic errors: wrong constant, init, or format",
        "Submissions with wrong variable initialization values, perturbed numeric constants, or wrong output string formatting.",
    ),
    "arithmetic_operator_error": (
        "Logic errors: arithmetic / update operator",
        "Submissions with a mutated arithmetic or compound assignment operator (+, -, *, /, %, +=, -=).",
    ),
    "loop_boundary_error": (
        "Logic errors: loop bounds / entry condition",
        "Submissions with off-by-one loop start/end bounds or inverted loop entry conditions that terminate with wrong output.",
    ),
    "missing_statement_error": (
        "Logic errors: missing update statement",
        "Submissions where a key state-update, accumulation, or step statement was omitted.",
    ),
}


@dataclass(frozen=True)
class Submission:
    record_id: int
    assignment_title: str
    category: str
    source_code: str
    standard_input: str
    raw_category: str = ""

    @property
    def normalized_category(self) -> str:
        return "logic_error" if self.category == "random_error" else self.category


def _normalize_code_lines(code: str) -> list[str]:
    out: list[str] = []
    for raw_line in code.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("//") or (stripped.startswith("/*") and stripped.endswith("*/")):
            continue
        stripped = re.sub(r"\s*\(\s*", "(", stripped)
        stripped = re.sub(r"\s*\)\s*", ")", stripped)
        stripped = re.sub(r"\s*\{\s*$", " {", stripped)
        out.append(stripped)
    merged: list[str] = []
    for line in out:
        if line == "{" and merged:
            merged[-1] = merged[-1] + " {"
        else:
            merged.append(line)
    return merged


def _build_reference_templates(records: list[Submission]) -> dict[str, list[tuple[str, ...]]]:
    templates: dict[str, list[tuple[str, ...]]] = collections.defaultdict(list)
    seen: dict[str, set[tuple[str, ...]]] = collections.defaultdict(set)
    for record in records:
        if (record.raw_category or record.category) != "correct":
            continue
        normalized = tuple(_normalize_code_lines(record.source_code))
        if normalized not in seen[record.assignment_title]:
            seen[record.assignment_title].add(normalized)
            templates[record.assignment_title].append(normalized)
    return templates


def _diff_against_templates(
    assignment_title: str,
    source_code: str,
    templates: dict[str, list[tuple[str, ...]]],
) -> tuple[list[str], list[str]]:
    candidate_lines = _normalize_code_lines(source_code)
    candidate_counter = collections.Counter(candidate_lines)
    best_removed: list[str] | None = None
    best_added: list[str] | None = None
    for template in templates.get(assignment_title, []):
        template_counter = collections.Counter(template)
        removed = list((template_counter - candidate_counter).elements())
        added = list((candidate_counter - template_counter).elements())
        if best_removed is None or (len(removed) + len(added)) < (len(best_removed) + len(best_added or [])):
            best_removed, best_added = removed, added
    return best_removed or [], best_added or []


def classify_submission(
    raw_category: str,
    assignment_title: str,
    source_code: str,
    templates: dict[str, list[tuple[str, ...]]],
) -> str:
    if raw_category in CATEGORY_DEFINITIONS and raw_category != "correct":
        return raw_category
    if raw_category == "correct":
        return "correct"

    removed, added = _diff_against_templates(assignment_title, source_code, templates)

    if raw_category == "compile_error":
        if any(re.search(r"\b(integer|itn|Int|chr|floating|doubl)\b", line) for line in added):
            return "compile_type_error"
        if (
            (removed and not added and any(r.startswith(("int ", "double ", "long ", "char ", "enum ", "size_t ")) for r in removed))
            or (
                len(removed) == 1
                and len(added) == 1
                and re.match(r"^(int|double|long|char|size_t)\s+\w+", removed[0]) is not None
                and re.match(r"^(int|double|long|char|size_t)\b", added[0]) is None
            )
            or any(re.search(r"\b(tep|emp|yaer|by100|ix|vx)\b", line) for line in added)
        ):
            return "compile_undeclared_error"
        return "compile_syntax_error"

    if raw_category == "infinite_loop":
        if not removed and added:
            return "infinite_loop_unconditional"
        return "infinite_loop_condition_bug"

    if raw_category == "pointer_error":
        if any("struct " in line or "->" in line for line in added):
            return "null_struct_pointer_deref"
        return "null_pointer_deref"

    if raw_category in {"random_error", "logic_error"}:
        if not removed and len(added) == 1 and added[0].startswith("return"):
            return "early_return_error"
        if len(removed) == 1 and len(added) == 1 and removed[0] == "return 0;" and added[0].startswith("return "):
            return "early_return_error"
        if removed and not added:
            return "missing_statement_error"
        if any(re.search(r"\b(for|while)\s*\(", line) for line in removed + added):
            return "loop_boundary_error"
        if any(re.search(r"\bif\s*\(", line) for line in removed + added):
            return "branch_condition_error"
        if len(removed) == 1 and len(added) == 1:
            rem_line, add_line = removed[0], added[0]
            if (
                re.sub(r"(==|!=|<=|>=|<|>|&&|\|\|)", "REL", rem_line)
                == re.sub(r"(==|!=|<=|>=|<|>|&&|\|\|)", "REL", add_line)
                and rem_line != add_line
            ):
                return "branch_condition_error"
            if (
                re.sub(r"(\+=|-=|\*=|/=|\+\+|--|[+\-*/%])", "ARITH", rem_line)
                == re.sub(r"(\+=|-=|\*=|/=|\+\+|--|[+\-*/%])", "ARITH", add_line)
                and rem_line != add_line
            ):
                return "arithmetic_operator_error"
        return "wrong_constant_or_init_error"

    return raw_category


def load_submissions(path: Path) -> list[Submission]:
    raw_records: list[Submission] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                item = json.loads(line)
                raw_cat = str(item["category"])
                raw_records.append(Submission(
                    record_id=int(item["recordId"]),
                    assignment_title=str(item["assignmentTitle"]),
                    category=raw_cat,
                    source_code=str(item["sourceCode"]),
                    standard_input=str(item.get("standardInput", "")),
                    raw_category=raw_cat,
                ))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid submission record at line {line_number}: {exc}") from exc

    templates = _build_reference_templates(raw_records)
    classified: list[Submission] = []
    for record in raw_records:
        fine_category = classify_submission(
            record.raw_category,
            record.assignment_title,
            record.source_code,
            templates,
        )
        classified.append(Submission(
            record_id=record.record_id,
            assignment_title=record.assignment_title,
            category=fine_category,
            source_code=record.source_code,
            standard_input=record.standard_input,
            raw_category=record.raw_category,
        ))
    return classified


def select_submissions(records: list[Submission], per_category: int, seed: int) -> list[Submission]:
    categories = sorted({record.category for record in records})
    selected: list[Submission] = []
    for category in categories:
        candidates = [record for record in records if record.category == category]
        if len(candidates) < per_category:
            raise ValueError(f"Category {category!r} has only {len(candidates)} records.")
        rng = random.Random(f"{seed}:{category}")
        selected.extend(sorted(rng.sample(candidates, per_category), key=lambda record: record.record_id))
    return selected


def dataset_hash(records: list[Submission]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(f"{record.record_id}\0{record.category}\0{record.source_code}\0".encode())
    return digest.hexdigest()


DATASET_SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS category_folders (
    category TEXT PRIMARY KEY,
    normalized_category TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    assignment_title TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    task_focus TEXT NOT NULL,
    api_assignment_id TEXT,
    standard_input_template TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS submissions (
    record_id INTEGER PRIMARY KEY,
    assignment_title TEXT NOT NULL,
    category TEXT NOT NULL,
    normalized_category TEXT NOT NULL,
    raw_category TEXT NOT NULL DEFAULT '',
    source_code TEXT NOT NULL,
    standard_input TEXT NOT NULL,
    folder_category TEXT NOT NULL,
    FOREIGN KEY (assignment_title) REFERENCES assignments(assignment_title),
    FOREIGN KEY (folder_category) REFERENCES category_folders(category)
);
CREATE INDEX IF NOT EXISTS idx_submissions_category ON submissions(category);
CREATE INDEX IF NOT EXISTS idx_submissions_assignment ON submissions(assignment_title);
"""


def infer_assignment_description(title: str) -> str:
    return (
        f"Complete the C programming assignment '{title}' using C11. "
        "Implement the required behavior, accept the assignment's standard input when applicable, "
        "and print exactly the expected program output."
    )


class DatasetStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(DATASET_SCHEMA)
            existing_cols = {
                row[1]
                for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
            }
            if "raw_category" not in existing_cols:
                connection.execute(
                    "ALTER TABLE submissions ADD COLUMN raw_category TEXT NOT NULL DEFAULT ''"
                )
            connection.commit()

    def rebuild(self, source_path: Path) -> str:
        records = load_submissions(source_path)
        source_hash = dataset_hash(records)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(DATASET_SCHEMA)
            existing_cols = {
                row[1]
                for row in connection.execute("PRAGMA table_info(submissions)").fetchall()
            }
            if "raw_category" not in existing_cols:
                connection.execute(
                    "ALTER TABLE submissions ADD COLUMN raw_category TEXT NOT NULL DEFAULT ''"
                )
            connection.execute("DELETE FROM submissions")
            connection.execute("DELETE FROM assignments")
            connection.execute("DELETE FROM category_folders")

            for category, (display_name, description) in CATEGORY_DEFINITIONS.items():
                connection.execute(
                    "INSERT INTO category_folders(category, normalized_category, display_name, description) "
                    "VALUES (?, ?, ?, ?)",
                    (category, category, display_name, description),
                )

            assignment_titles = sorted({record.assignment_title for record in records})
            for title in assignment_titles:
                connection.execute(
                    "INSERT INTO assignments(assignment_title, description, task_focus) "
                    "VALUES (?, ?, ?)",
                    (title, infer_assignment_description(title), title),
                )

            connection.executemany(
                "INSERT INTO submissions("
                "record_id, assignment_title, category, normalized_category, raw_category, "
                "source_code, standard_input, folder_category"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record.record_id,
                        record.assignment_title,
                        record.category,
                        record.normalized_category,
                        record.raw_category or record.category,
                        record.source_code,
                        record.standard_input,
                        record.category,
                    )
                    for record in records
                ],
            )
            connection.execute(
                "INSERT OR REPLACE INTO dataset_metadata(key, value) VALUES ('source_hash', ?)",
                (source_hash,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO dataset_metadata(key, value) VALUES ('source_path', ?)",
                (str(source_path.resolve()),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO dataset_metadata(key, value) VALUES ('record_count', ?)",
                (str(len(records)),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO dataset_metadata(key, value) VALUES ('category_count', ?)",
                (str(len(CATEGORY_DEFINITIONS)),),
            )
            connection.commit()
        return source_hash

    def select(self, per_category: int, seed: int) -> list[Submission]:
        with sqlite3.connect(self.path) as connection:
            categories = [
                row[0] for row in connection.execute(
                    "SELECT category FROM category_folders ORDER BY category"
                )
            ]
            selected: list[Submission] = []
            for category in categories:
                candidates = [
                    Submission(
                        record_id=row[0],
                        assignment_title=row[1],
                        category=row[2],
                        source_code=row[3],
                        standard_input=row[4],
                        raw_category=row[5] or row[2],
                    )
                    for row in connection.execute(
                        "SELECT record_id, assignment_title, category, source_code, standard_input, raw_category "
                        "FROM submissions WHERE category=?",
                        (category,),
                    )
                ]
                if category == "infinite_loop_condition_bug":
                    non_flooding = [
                        item for item in candidates
                        if item.assignment_title != "09. Star triangle (L4)"
                    ]
                    if len(non_flooding) >= per_category:
                        candidates = non_flooding
                if len(candidates) < per_category:
                    raise ValueError(f"Category {category!r} has only {len(candidates)} records.")
                rng = random.Random(f"{seed}:{category}")
                selected.extend(sorted(rng.sample(candidates, per_category), key=lambda item: item.record_id))
            return selected

    def load_assignment_details(self) -> dict[str, dict[str, str]]:
        with sqlite3.connect(self.path) as connection:
            return {
                str(row[0]): {
                    "description": str(row[1] or ""),
                    "task_focus": str(row[2] or ""),
                }
                for row in connection.execute(
                    "SELECT assignment_title, description, task_focus FROM assignments"
                ).fetchall()
            }

    def sync_api_assignments(self, assignments: dict[str, dict]) -> None:
        with sqlite3.connect(self.path) as connection:
            for title, details in assignments.items():
                connection.execute(
                    "UPDATE assignments SET api_assignment_id=?, description=?, task_focus=? "
                    "WHERE assignment_title=?",
                    (
                        str(details.get("id", "")),
                        str(details.get("description") or infer_assignment_description(title)),
                        str(details.get("taskFocus") or title),
                        title,
                    ),
                )
