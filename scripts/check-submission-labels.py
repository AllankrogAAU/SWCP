#!/usr/bin/env python3
"""Check JSONL submission labels against SWCP sandbox results via the live API."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VALID_CATEGORIES = {"correct", "random_error", "infinite_loop", "pointer_error", "compile_error"}
# The sandbox reports these common fatal signals as shell exit codes (128 + signal).
SIGNAL_EXIT_CODES = {132, 134, 135, 139, -4, -6, -7, -11}
CRASH_OUTPUT = re.compile(
    r"segmentation fault|bus error|addresssanitizer|invalid (?:read|write|pointer)",
    re.IGNORECASE,
)
# LeakSanitizer reports end with "SUMMARY: AddressSanitizer: N byte(s) leaked"; a leak is not a crash.
LEAK_REPORT_LINE = re.compile(r"^.*(?:LeakSanitizer|byte\(s\) leaked).*$", re.IGNORECASE | re.MULTILINE)
REPORT_FIELDS = [
    "recordId",
    "assignmentTitle",
    "expectedCategory",
    "observedCategory",
    "matches",
    "checkStatus",
    "submissionId",
    "pipelineStatus",
    "errorClassification",
    "compileSuccess",
    "passedTests",
    "totalTests",
    "timedOutTests",
    "crashedTests",
    "evidence",
]


def parse_args() -> argparse.Namespace:
    default_dataset = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "sample-submissions-11000-with-compile-errors.jsonl"
    )
    parser = argparse.ArgumentParser(
        description="Submit a JSONL dataset to SWCP and compare category labels with sandbox behavior."
    )
    parser.add_argument("--dataset", type=Path, default=default_dataset)
    parser.add_argument("--report", type=Path, help="CSV report path (defaults beside the dataset)")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("SWCP_API_URL", "http://swcp.local"),
    )
    parser.add_argument("--user-prefix", default="swcp-label-check")
    parser.add_argument(
        "--password",
        default=os.environ.get("SWCP_TEST_PASSWORD", "test-123"),
        help="test-account password (or set SWCP_TEST_PASSWORD)",
    )
    parser.add_argument("--students", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--backend", choices=("local", "azure"), default="local")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--offset", type=int, default=0, help="skip this many records before checking")
    parser.add_argument("--limit", type=int, help="check at most this many records")
    parser.add_argument("--dry-run", action="store_true", help="validate and summarize without calling the API")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation before live submissions")
    args = parser.parse_args()

    if args.students < 1 or args.concurrency < 1:
        parser.error("--students and --concurrency must be positive")
    if args.poll_seconds <= 0 or args.timeout_seconds < 1:
        parser.error("poll and timeout values must be positive")
    if args.offset < 0 or (args.limit is not None and args.limit < 1):
        parser.error("--offset must be non-negative and --limit must be positive")
    if not args.password:
        parser.error("password must not be empty")
    username_suffix_width = max(2, len(str(args.students)))
    if not args.user_prefix or len(args.user_prefix) + username_suffix_width > 32:
        parser.error("--user-prefix must leave room for numbered usernames of at most 32 characters")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.user_prefix):
        parser.error("--user-prefix may contain only letters, numbers, '.', '_' and '-'")
    if args.report is None:
        args.report = (
            Path.home() / "Downloads" / f"{args.dataset.stem}-label-check.csv"
        )
    return args


def load_dataset(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise RuntimeError(f"Dataset file not found: {path}")

    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with path.open(encoding="utf-8") as dataset:
        for line_number, line in enumerate(dataset, start=1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"Invalid JSON on line {line_number}: {error}") from error
            if not isinstance(row, dict):
                raise RuntimeError(f"Dataset line {line_number} must contain a JSON object.")

            record_id = row.get("recordId", line_number)
            if not isinstance(record_id, (str, int)):
                raise RuntimeError(f"recordId on line {line_number} must be a string or integer.")
            record_id = str(record_id)
            if record_id in seen_ids:
                raise RuntimeError(f"Duplicate recordId {record_id!r} on line {line_number}.")
            seen_ids.add(record_id)

            title = row.get("assignmentTitle")
            category = row.get("category")
            source = row.get("sourceCode")
            if not isinstance(title, str) or not title.strip():
                raise RuntimeError(f"Missing assignmentTitle on line {line_number}.")
            if not isinstance(category, str) or category not in VALID_CATEGORIES:
                raise RuntimeError(
                    f"Unsupported category {category!r} on line {line_number}; "
                    f"expected one of {', '.join(sorted(VALID_CATEGORIES))}."
                )
            if not isinstance(source, str) or not source.strip():
                raise RuntimeError(f"Missing sourceCode on line {line_number}.")

            rows.append({**row, "recordId": record_id})

    if not rows:
        raise RuntimeError("Dataset contains no records.")
    return rows


def api_request(
    base_url: str,
    path: str,
    method: str = "GET",
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(base_url.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            try:
                parsed = json.loads(payload) if payload else None
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"API returned invalid JSON for {method} {path}: {error}"
                ) from error
            return response.status, parsed
    except HTTPError as error:
        payload = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload) if payload else None
        except json.JSONDecodeError:
            parsed = payload
        return error.code, parsed
    except URLError as error:
        raise RuntimeError(f"Could not reach {base_url}: {error.reason}") from error


def student_token(base_url: str, username: str, password: str) -> str:
    status, result = api_request(
        base_url,
        "/api/auth/register",
        "POST",
        body={"username": username, "password": password},
    )
    if status not in (201, 409):
        raise RuntimeError(f"Could not create student {username!r} (HTTP {status}): {result}")

    status, result = api_request(
        base_url,
        "/api/auth/login",
        "POST",
        body={"username": username, "password": password},
    )
    if status != 200 or not isinstance(result, dict) or not result.get("accessToken"):
        raise RuntimeError(f"Could not log in student {username!r} (HTTP {status}): {result}")
    return result["accessToken"]


def poll_submission(
    base_url: str,
    token: str,
    submission_id: str,
    poll_seconds: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, result = api_request(
            base_url, f"/api/submissions/{submission_id}", token=token
        )
        if status != 200 or not isinstance(result, dict):
            raise RuntimeError(
                f"Could not read submission {submission_id} (HTTP {status}): {result}"
            )
        if result.get("status") in ("COMPLETED", "FAILED"):
            return result
        time.sleep(poll_seconds)
    raise TimeoutError(
        f"Submission {submission_id} did not finish within {timeout_seconds}s."
    )


def _is_crash(test_result: dict[str, Any]) -> bool:
    exit_code = test_result.get("exitCode")
    stderr = LEAK_REPORT_LINE.sub("", str(test_result.get("stderr") or ""))
    return exit_code in SIGNAL_EXIT_CODES or bool(CRASH_OUTPUT.search(stderr))


def classify_sandbox_output(
    sandbox: dict[str, Any], expected_test_count: int
) -> tuple[str, dict[str, Any]]:
    """Infer the dataset category from observed execution, not the LLM's taskSolved value."""
    compilation = sandbox.get("compilation")
    test_results = sandbox.get("testResults")
    error_classification = str(sandbox.get("errorClassification") or "")
    # A failed compile usually comes back without test results, so it is checked before requiring them.
    if isinstance(compilation, dict) and compilation.get("success") is not True:
        tests = test_results if isinstance(test_results, list) else []
        return "compile_error", {
            "errorClassification": error_classification,
            "compileSuccess": False,
            "passedTests": sum(isinstance(t, dict) and t.get("passed") is True for t in tests),
            "totalTests": len(tests),
            "timedOutTests": 0,
            "crashedTests": 0,
            "evidence": str(compilation.get("stderr") or "Compilation failed.")[:500],
        }
    if not isinstance(compilation, dict) or not isinstance(test_results, list):
        return "incomplete_results", {
            "errorClassification": error_classification,
            "compileSuccess": "",
            "passedTests": 0,
            "totalTests": 0,
            "timedOutTests": 0,
            "crashedTests": 0,
            "evidence": "Sandbox output is missing compilation or test results.",
        }

    compile_success = compilation.get("success") is True
    passed_tests = sum(test.get("passed") is True for test in test_results if isinstance(test, dict))
    timed_out_tests = sum(
        test.get("exitCode") == 124
        or "timed out" in str(test.get("stderr") or "").lower()
        for test in test_results
        if isinstance(test, dict)
    )
    crashed_tests = sum(
        _is_crash(test) for test in test_results if isinstance(test, dict)
    )
    metrics = {
        "errorClassification": error_classification,
        "compileSuccess": compile_success,
        "passedTests": passed_tests,
        "totalTests": len(test_results),
        "timedOutTests": timed_out_tests,
        "crashedTests": crashed_tests,
    }

    # Compile failures and incomplete runs are reported separately because they don't prove a label.
    if not compile_success:
        return "compile_error", {**metrics, "evidence": compilation.get("stderr") or "Compilation failed."}

    # A test timeout is the observable effect used for the infinite_loop label.
    if error_classification.split(";", maxsplit=1)[0].strip().lower() == "run_timeout" or timed_out_tests:
        return "infinite_loop", {**metrics, "evidence": "At least one test timed out."}

    # Fatal signal exit codes or crash diagnostics distinguish pointer faults from ordinary wrong answers.
    if crashed_tests:
        return "pointer_error", {**metrics, "evidence": "At least one test crashed or reported an invalid memory access."}

    if len(test_results) != expected_test_count:
        return "incomplete_results", {
            **metrics,
            "evidence": f"Expected {expected_test_count} test results but received {len(test_results)}.",
        }

    # A correct label requires every assignment test to pass.
    if expected_test_count > 0 and passed_tests == expected_test_count:
        return "correct", {**metrics, "evidence": "All assignment tests passed."}

    # Any other completed, non-crashing test failure is treated as random_error.
    return "random_error", {
        **metrics,
        "evidence": f"{expected_test_count - passed_tests} assignment test(s) failed without a timeout or crash.",
    }


def completed_record_ids(report_path: Path) -> set[str]:
    if not report_path.is_file():
        return set()
    with report_path.open(encoding="utf-8", newline="") as report:
        return {
            row["recordId"]
            for row in csv.DictReader(report)
            if row.get("checkStatus") == "checked" and row.get("recordId")
        }


def make_report_row(
    record: dict[str, Any],
    observed: str,
    check_status: str,
    submission_id: str = "",
    pipeline_status: str = "",
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metrics = metrics or {}
    expected = record["category"]
    matches = observed == expected
    return {
        "recordId": record["recordId"],
        "assignmentTitle": record["assignmentTitle"],
        "expectedCategory": expected,
        "observedCategory": observed,
        "matches": matches,
        "checkStatus": check_status,
        "submissionId": submission_id,
        "pipelineStatus": pipeline_status,
        "errorClassification": metrics.get("errorClassification", ""),
        "compileSuccess": metrics.get("compileSuccess", ""),
        "passedTests": metrics.get("passedTests", ""),
        "totalTests": metrics.get("totalTests", ""),
        "timedOutTests": metrics.get("timedOutTests", ""),
        "crashedTests": metrics.get("crashedTests", ""),
        "evidence": metrics.get("evidence", ""),
    }


def check_record(
    base_url: str,
    token: str,
    record: dict[str, Any],
    assignment_id: str,
    expected_test_count: int,
    backend: str,
    poll_seconds: float,
    timeout_seconds: int,
) -> dict[str, Any]:
    status, accepted = api_request(
        base_url,
        "/api/submissions/",
        "POST",
        token=token,
        body={
            "assignmentId": assignment_id,
            "sourceCode": record["sourceCode"],
            "llmBackend": backend,
            # "submit" runs the assignment's full test suite; "run" only executes supplied stdin.
            "action": "submit",
        },
    )
    if status != 202 or not isinstance(accepted, dict) or not accepted.get("submissionId"):
        raise RuntimeError(
            f"Submission rejected for record {record['recordId']} "
            f"(HTTP {status}): {accepted}"
        )

    submission_id = accepted["submissionId"]
    result = poll_submission(
        base_url, token, submission_id, poll_seconds, timeout_seconds
    )
    sandbox = result.get("sandboxOutput")
    if not isinstance(sandbox, dict):
        raise RuntimeError(
            f"Submission {submission_id} finished as {result.get('status')} "
            f"without sandbox output: {result.get('errorMessage')}"
        )

    observed, metrics = classify_sandbox_output(sandbox, expected_test_count)
    return make_report_row(
        record,
        observed,
        "checked",
        submission_id,
        result.get("status", ""),
        metrics,
    )


def main() -> int:
    args = parse_args()
    try:
        rows = load_dataset(args.dataset)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    rows = rows[args.offset :]
    if args.limit is not None:
        rows = rows[: args.limit]
    if not rows:
        print("No records selected.")
        return 0

    counts = Counter(row["category"] for row in rows)
    print(f"Dataset: {args.dataset}")
    print(f"Selected records: {len(rows)}")
    print("Labels: " + ", ".join(f"{label}={counts[label]}" for label in sorted(VALID_CATEGORIES)))
    if args.dry_run:
        print("Dry run: dataset validated; no API requests were made.")
        return 0

    already_checked = completed_record_ids(args.report)
    pending = [row for row in rows if row["recordId"] not in already_checked]
    if not pending:
        print(f"All selected records already have results in {args.report}.")
        return 0

    worker_count = min(args.concurrency, args.students, len(pending))
    student_count = min(args.students, len(pending))
    print(
        f"This will submit {len(pending)} code samples to {args.api_url} using "
        f"{worker_count} concurrent students and the {args.backend!r} LLM backend."
    )
    print(
        "Each request uses /api/submissions/, which runs the assignment tests and also "
        "queues LLM feedback. This can take a long time and may incur model costs."
    )
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled; no submissions were sent.")
        return 0

    tokens = [""] * student_count
    token_workers = min(worker_count, student_count, 8)
    width = max(2, len(str(student_count)))
    try:
        with ThreadPoolExecutor(max_workers=token_workers) as executor:
            token_futures = {
                executor.submit(
                    student_token,
                    args.api_url,
                    f"{args.user_prefix}{index + 1:0{width}d}",
                    args.password,
                ): index
                for index in range(student_count)
            }
            for future in as_completed(token_futures):
                tokens[token_futures[future]] = future.result()

        status, assignment_rows = api_request(
            args.api_url, "/api/assignments/", token=tokens[0]
        )
        if status != 200 or not isinstance(assignment_rows, list):
            raise RuntimeError(f"Could not list assignments (HTTP {status}): {assignment_rows}")
        assignment_ids = {
            row.get("title"): row.get("id")
            for row in assignment_rows
            if isinstance(row, dict)
        }
        used_titles = {row["assignmentTitle"] for row in pending}
        missing = sorted(title for title in used_titles if not assignment_ids.get(title))
        if missing:
            raise RuntimeError("The API is missing dataset assignments: " + ", ".join(missing))

        test_counts: dict[str, int] = {}
        for title in used_titles:
            detail_status, detail = api_request(
                args.api_url,
                f"/api/assignments/{assignment_ids[title]}",
                token=tokens[0],
            )
            cases = detail.get("testCases") if isinstance(detail, dict) else None
            if detail_status != 200 or not isinstance(cases, list) or not cases:
                raise RuntimeError(
                    f"Assignment {title!r} has no readable test cases "
                    f"(HTTP {detail_status})."
                )
            test_counts[title] = len(cases)

        student_jobs: list[list[dict[str, Any]]] = [[] for _ in tokens]
        for index, row in enumerate(pending):
            student_jobs[index % student_count].append(row)

        args.report.parent.mkdir(parents=True, exist_ok=True)
        report_exists = args.report.exists() and args.report.stat().st_size > 0
        with args.report.open("a", encoding="utf-8", newline="") as report_file:
            writer = csv.DictWriter(report_file, fieldnames=REPORT_FIELDS)
            if not report_exists:
                writer.writeheader()
                report_file.flush()

            report_lock = threading.Lock()

            def save_row(row: dict[str, Any]) -> None:
                with report_lock:
                    writer.writerow(row)
                    report_file.flush()

            def run_student(index: int) -> list[dict[str, Any]]:
                outcomes = []
                for job_index, record in enumerate(student_jobs[index]):
                    try:
                        outcome = check_record(
                            args.api_url,
                            tokens[index],
                            record,
                            assignment_ids[record["assignmentTitle"]],
                            test_counts[record["assignmentTitle"]],
                            args.backend,
                            args.poll_seconds,
                            args.timeout_seconds,
                        )
                    except Exception as error:
                        outcome = make_report_row(
                            record,
                            "checker_error",
                            "checker_error",
                            metrics={"evidence": str(error)},
                        )
                        save_row(outcome)
                        outcomes.append(outcome)
                        print(
                            f"{record['recordId']}: checker error: {error}; "
                            f"stopping this student's queue "
                            f"({len(student_jobs[index]) - job_index - 1} records left unsubmitted)",
                            file=sys.stderr,
                        )
                        # The accepted submission may still be active; don't submit more work for this student.
                        break

                    save_row(outcome)
                    outcomes.append(outcome)
                    print(
                        f"{record['recordId']}: expected={outcome['expectedCategory']} "
                        f"observed={outcome['observedCategory']} "
                        f"matches={outcome['matches']} ({outcome['submissionId']})"
                    )
                return outcomes

            outcomes: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(run_student, index) for index in range(student_count)]
                for future in as_completed(futures):
                    outcomes.extend(future.result())

    except (RuntimeError, TimeoutError, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            print("Interrupted; already accepted submissions will continue in SWCP.", file=sys.stderr)
        else:
            print(f"Error: {error}", file=sys.stderr)
        print(f"Any completed checks are saved in {args.report}.")
        return 1

    checked = [row for row in outcomes if row["checkStatus"] == "checked"]
    matches = sum(row["matches"] is True for row in checked)
    mismatches = len(checked) - matches
    errors = len(outcomes) - len(checked)
    print(
        f"Checked {len(checked)} records: matches={matches}, mismatches={mismatches}, "
        f"checker_errors={errors}."
    )
    print(f"Report: {args.report}")
    return 1 if mismatches or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())