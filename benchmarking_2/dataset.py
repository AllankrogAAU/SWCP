from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Submission:
    record_id: int
    assignment_title: str
    category: str
    source_code: str
    standard_input: str

    @property
    def normalized_category(self) -> str:
        return "logic_error" if self.category == "random_error" else self.category


def load_submissions(path: Path) -> list[Submission]:
    records: list[Submission] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                item = json.loads(line)
                records.append(Submission(
                    record_id=int(item["recordId"]),
                    assignment_title=str(item["assignmentTitle"]),
                    category=str(item["category"]),
                    source_code=str(item["sourceCode"]),
                    standard_input=str(item.get("standardInput", "")),
                ))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"Invalid submission record at line {line_number}: {exc}") from exc
    return records


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
        digest.update(f"{record.record_id}\0{record.source_code}\0".encode())
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

    def rebuild(self, source_path: Path) -> str:
        records = load_submissions(source_path)
        source_hash = dataset_hash(records)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(DATASET_SCHEMA)
            connection.execute("DELETE FROM submissions")
            connection.execute("DELETE FROM assignments")
            connection.execute("DELETE FROM category_folders")

            category_data = {
                "correct": ("Correct submissions", "Working solutions used to measure false positives."),
                "compile_error": ("Compile errors", "Submissions that fail compilation."),
                "infinite_loop": ("Infinite loops", "Submissions that do not terminate."),
                "pointer_error": ("Pointer errors", "Submissions with invalid pointer usage."),
                "random_error": (
                    "Logic and miscellaneous errors",
                    "Heterogeneous incorrect submissions; evaluated as the broad logic_error category.",
                ),
            }
            for category, (display_name, description) in category_data.items():
                normalized = "logic_error" if category == "random_error" else category
                connection.execute(
                    "INSERT INTO category_folders(category, normalized_category, display_name, description) "
                    "VALUES (?, ?, ?, ?)",
                    (category, normalized, display_name, description),
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
                "record_id, assignment_title, category, normalized_category, source_code, "
                "standard_input, folder_category"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        record.record_id,
                        record.assignment_title,
                        record.category,
                        record.normalized_category,
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
                    )
                    for row in connection.execute(
                        "SELECT record_id, assignment_title, category, source_code, standard_input "
                        "FROM submissions WHERE category=?",
                        (category,),
                    )
                ]
                if len(candidates) < per_category:
                    raise ValueError(f"Category {category!r} has only {len(candidates)} records.")
                rng = random.Random(f"{seed}:{category}")
                selected.extend(sorted(rng.sample(candidates, per_category), key=lambda item: item.record_id))
            return selected

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
