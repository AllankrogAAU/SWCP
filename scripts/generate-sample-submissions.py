#!/usr/bin/env python3
"""Submit a balanced set of introductory-C examples through the SWCP API."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from fractions import Fraction
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STUDENT_PASSWORD = "test-123"


@dataclass(frozen=True)
class Example:
    title: str
    entry: str
    return_kind: str
    source: str
    run_stdin: str = ""


EXAMPLES = [
    Example("01. Hello, kursus (L1)", "main", "main", r"""
#include <stdio.h>
int main(void) {
    puts("Hello, kursus! C programming.");
    return 0;
}
"""),
    Example("02. Temperature converter (L2)", "celsius_to_fahrenheit", "double", r"""
double celsius_to_fahrenheit(double c) {
    return c * 9.0 / 5.0 + 32.0;
}
""", "0\n"),
    Example("03. Seconds to h/m/s (L2)", "main", "main", r"""
#include <stdio.h>
int main(void) {
    int total, hours, minutes, seconds;
    if (scanf("%d", &total) != 1 || total < 0) return 1;
    hours = total / 3600;
    minutes = (total % 3600) / 60;
    seconds = total % 60;
    printf("%d hours, %d minutes, %d seconds\n", hours, minutes, seconds);
    return 0;
}
""", "3661\n"),
    Example("04. Leap year (L3)", "is_leap_year", "int", r"""
int is_leap_year(int year) {
    return year % 400 == 0 || (year % 4 == 0 && year % 100 != 0);
}
""", "2000\n"),
    Example("05. 7-trinsskala (L3)", "grade_from_points", "int", r"""
int grade_from_points(int points) {
    if (points >= 90) return 12;
    if (points >= 80) return 10;
    if (points >= 70) return 7;
    if (points >= 60) return 4;
    if (points >= 50) return 2;
    if (points >= 40) return 0;
    return -3;
}
""", "95\n"),
    Example("06. Switch calculator (L3)", "calc", "int", r"""
int calc(int a, char op, int b) {
    switch (op) {
        case '+': return a + b;
        case '-': return a - b;
        case '*': return a * b;
        case '/': return b == 0 ? 0 : a / b;
        default: return 0;
    }
}
""", "8 + 2\n"),
    Example("07. Digit sum (L4)", "digit_sum", "int", r"""
int digit_sum(int n) {
    long long value = n;
    int sum = 0;
    if (value < 0) value = -value;
    do {
        sum += (int)(value % 10);
        value /= 10;
    } while (value > 0);
    return sum;
}
""", "1234\n"),
    Example("08. Prime check (L4)", "is_prime", "int", r"""
int is_prime(int n) {
    if (n < 2) return 0;
    for (int divisor = 2; divisor <= n / divisor; ++divisor) {
        if (n % divisor == 0) return 0;
    }
    return 1;
}
""", "2\n"),
    Example("09. Star triangle (L4)", "main", "main", r"""
#include <stdio.h>
int main(void) {
    int n;
    if (scanf("%d", &n) != 1 || n < 1) return 1;
    for (int row = 1; row <= n; ++row) {
        for (int column = 0; column < row; ++column) putchar('*');
        putchar('\n');
    }
    return 0;
}
""", "3\n"),
    Example("10. Max of three (L5)", "max3", "int", r"""
static int max2(int a, int b) {
    return a > b ? a : b;
}
int max3(int a, int b, int c) {
    return max2(max2(a, b), c);
}
""", "3 8 5\n"),
    Example("11. GCD (L5)", "gcd", "int", r"""
int gcd(int a, int b) {
    long long left = a, right = b;
    if (left < 0) left = -left;
    if (right < 0) right = -right;
    while (right != 0) {
        long long remainder = left % right;
        left = right;
        right = remainder;
    }
    return (int)left;
}
""", "48 18\n"),
    Example("12. Power without pow (L5)", "power", "long", r"""
long power(int base, int exp) {
    long result = 1;
    if (exp < 0) return 0;
    for (int i = 0; i < exp; ++i) result *= base;
    return result;
}
""", "2 10\n"),
    Example("13. Quadratic roots (L6)", "solve_quadratic", "int", r"""
static double square_root(double value) {
    double estimate = value > 1.0 ? value : 1.0;
    for (int i = 0; i < 32; ++i) estimate = (estimate + value / estimate) / 2.0;
    return estimate;
}
int solve_quadratic(double a, double b, double c, double *r1, double *r2) {
    if (a == 0.0) {
        if (b == 0.0) return 0;
        *r1 = -c / b;
        return 1;
    }
    double discriminant = b * b - 4.0 * a * c;
    if (discriminant < 0.0) return 0;
    if (discriminant == 0.0) {
        *r1 = -b / (2.0 * a);
        return 1;
    }
    double root = square_root(discriminant);
    double first = (-b - root) / (2.0 * a);
    double second = (-b + root) / (2.0 * a);
    *r1 = first < second ? first : second;
    *r2 = first < second ? second : first;
    return 2;
}
""", "1 0 -1\n"),
    Example("14. ID generator (L6)", "next_id", "int", r"""
int next_id(void) {
    static int id = 0;
    return ++id;
}
"""),
    Example("15. Defensive BMI (L7)", "bmi", "double", r"""
double bmi(double kg, double height_m) {
    if (kg <= 0.0 || height_m <= 0.0) return -1.0;
    return kg / (height_m * height_m);
}
""", "70 1.75\n"),
    Example("16. Weekday enum (L8)", "next_day", "enum", r"""
enum weekday { Sunday = 0, Monday, Tuesday, Wednesday, Thursday, Friday, Saturday };
enum weekday next_day(enum weekday d) {
    return d == Saturday ? Sunday : (enum weekday)(d + 1);
}
""", "0\n"),
    Example("17. Exact average (L8)", "average3", "double", r"""
double average3(int a, int b, int c) {
    return ((double)a + b + c) / 3.0;
}
""", "1 2 2\n"),
    Example("18. Array max (L9)", "array_max", "int", r"""
int array_max(const int a[], int n) {
    int maximum = a[0];
    for (int i = 1; i < n; ++i) {
        if (a[i] > maximum) maximum = a[i];
    }
    return maximum;
}
""", "4 1 8 2 -3\n"),
    Example("19. Reverse in place (L9)", "reverse", "void", r"""
void reverse(int a[], int n) {
    for (int left = 0, right = n - 1; left < right; ++left, --right) {
        int temporary = a[left];
        a[left] = a[right];
        a[right] = temporary;
    }
}
""", "4 1 2 3 4\n"),
    Example("20. Linear search (L9)", "find_index", "int", r"""
int find_index(const int a[], int n, int target) {
    for (int i = 0; i < n; ++i) {
        if (a[i] == target) return i;
    }
    return -1;
}
""", "4 2 4 2 7 2\n"),
    Example("21. Dynamic range (L9)", "make_range", "pointer", r"""
#include <stdlib.h>
int *make_range(int n) {
    if (n <= 0) return NULL;
    int *values = malloc((size_t)n * sizeof *values);
    if (values == NULL) return NULL;
    for (int i = 0; i < n; ++i) values[i] = i + 1;
    return values;
}
""", "4\n"),
    Example("22. Own strlen (L10)", "my_strlen", "int", r"""
int my_strlen(const char *s) {
    int length = 0;
    while (s[length] != '\0') ++length;
    return length;
}
""", "hello\n"),
    Example("23. Palindrome (L10)", "is_palindrome", "int", r"""
int is_palindrome(const char *s) {
    int left = 0, right = 0;
    while (s[right] != '\0') ++right;
    if (right > 0) --right;
    while (left < right) {
        if (s[left] != s[right]) return 0;
        ++left;
        --right;
    }
    return 1;
}
""", "radar\n"),
    Example("24. Factorial (L11)", "factorial", "long", r"""
long factorial(int n) {
    if (n < 0) return 0;
    if (n <= 1) return 1;
    return n * factorial(n - 1);
}
""", "0\n"),
    Example("25. Fibonacci (L11)", "fib", "int", r"""
int fib(int n) {
    if (n <= 0) return 0;
    if (n == 1) return 1;
    return fib(n - 1) + fib(n - 2);
}
""", "0\n"),
]

CATEGORY_RATIOS = [
    ("correct", 30),
    ("random_error", 50),
    ("infinite_loop", 10),
    ("memory_leak", 5),
    ("pointer_error", 5),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or submit generated introductory-C examples through the live SWCP pipeline."
    )
    parser.add_argument("--api-url", default=os.environ.get("SWCP_API_URL", "http://swcp.local"))
    parser.add_argument("--count", type=int, default=300, help="total submissions to send (default: 300)")
    parser.add_argument("--students", type=int, default=25, help="student accounts in the reusable pool")
    parser.add_argument("--concurrency", type=int, default=12, help="students submitting simultaneously")
    parser.add_argument(
        "--action",
        choices=("submit", "run"),
        default="submit",
        help="submit asks for LLM feedback; run executes in the sandbox without LLM feedback",
    )
    parser.add_argument("--user-prefix", default="swcp-load")
    parser.add_argument("--backend", choices=("local", "azure"), default="local")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=20261115, help="random seed for reproducible variants")
    parser.add_argument("--dry-run", action="store_true", help="show planned counts without calling the API")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation before live submissions")
    args = parser.parse_args()

    if args.count < 1:
        parser.error("--count must be positive")
    if args.students < 1:
        parser.error("--students must be positive")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    if args.timeout_seconds < 1 or args.poll_seconds <= 0:
        parser.error("poll and timeout values must be positive")
    if not args.user_prefix or len(args.user_prefix) + len(str(args.students)) > 32:
        parser.error("--user-prefix must leave room for numbered usernames of at most 32 characters")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.user_prefix):
        parser.error("--user-prefix may contain only letters, numbers, '.', '_' and '-'")
    return args


def planned_counts(count: int) -> dict[str, int]:
    floors: dict[str, int] = {}
    remainders: list[tuple[Fraction, str]] = []
    category_order = {category: index for index, (category, _) in enumerate(CATEGORY_RATIOS)}
    for category, ratio in CATEGORY_RATIOS:
        exact = Fraction(count * ratio, 100)
        floors[category] = exact.numerator // exact.denominator
        remainders.append((exact - floors[category], category))

    unassigned = count - sum(floors.values())
    for _, category in sorted(
        remainders, key=lambda item: (-item[0], category_order[item[1]])
    )[:unassigned]:
        floors[category] += 1
    return floors


def build_jobs(count: int, rng: random.Random) -> list[tuple[Example, str]]:
    category_list: list[str] = []
    for category, amount in planned_counts(count).items():
        category_list.extend([category] * amount)
    rng.shuffle(category_list)

    base, remainder = divmod(count, len(EXAMPLES))
    assignment_list = [
        example
        for index, example in enumerate(EXAMPLES)
        for _ in range(base + (1 if index < remainder else 0))
    ]
    return list(zip(assignment_list, category_list, strict=True))


def inject_at_entry(source: str, entry: str, statement: str) -> str:
    match = re.search(rf"\b{re.escape(entry)}\s*\([^)]*\)\s*\{{", source)
    if match is None:
        raise ValueError(f"Could not locate function {entry!r} in the sample source")
    return source[: match.end()] + "\n    " + statement + source[match.end() :]


def make_source(example: Example, category: str, rng: random.Random) -> str:
    source = example.source.strip() + "\n"
    if category == "correct":
        return source
    if category == "random_error":
        if example.return_kind == "main":
            value = "0"
        elif example.return_kind == "void":
            value = None
        elif example.return_kind == "pointer":
            value = "NULL"
        elif example.return_kind == "enum":
            value = "Sunday"
        elif example.return_kind == "double":
            value = rng.choice(("0.0", "1.0", "-1.0"))
        else:
            value = rng.choice(("0", "1", "-1", "42"))
        statement = "return;" if value is None else f"return {value};"
        if value == "NULL" and "#include <stdlib.h>" not in source:
            source = "#include <stdlib.h>\n" + source
        return inject_at_entry(source, example.entry, statement)

    if category == "infinite_loop":
        return inject_at_entry(source, example.entry, "for (;;) {}")
    if category == "memory_leak":
        if "#include <stdlib.h>" not in source:
            source = "#include <stdlib.h>\n" + source
        return inject_at_entry(source, example.entry, "void *leaked = malloc(1024); (void)leaked;")
    if category == "pointer_error":
        source = "#include <stdlib.h>\n" + source
        return inject_at_entry(
            source,
            example.entry,
            "volatile int *bad_pointer = NULL; *bad_pointer = 1;",
        )
    raise ValueError(f"Unknown submission category: {category}")


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
            return response.status, json.loads(payload) if payload else None
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
    raise TimeoutError(f"Submission {submission_id} did not finish within {timeout_seconds}s.")


def print_elapsed(started_at: float) -> None:
    elapsed_seconds = int(time.monotonic() - started_at)
    hours, remainder = divmod(elapsed_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    print(f"Elapsed time: {hours:02d}:{minutes:02d}:{seconds:02d}")


def main() -> int:
    started_at = time.monotonic()
    args = parse_args()
    rng = random.Random(args.seed)
    jobs = build_jobs(args.count, rng)
    planned_jobs = [
        (example, category, make_source(example, category, rng))
        for example, category in jobs
    ]
    assignment_counts: dict[str, int] = {}
    for example, _, _ in planned_jobs:
        assignment_counts[example.title] = assignment_counts.get(example.title, 0) + 1

    print(f"Planned submissions: {args.count}")
    print("Categories: " + ", ".join(f"{name}={amount}" for name, amount in planned_counts(args.count).items()))
    assignment_distribution = list(assignment_counts.values())
    print(
        f"Per assignment: {min(assignment_distribution)}-{max(assignment_distribution)} "
        + f"({len(EXAMPLES)} assignments)"
    )
    print(f"Student pool: {min(args.students, args.count)}; concurrency: {min(args.concurrency, args.students, args.count)}")
    if args.dry_run:
        print_elapsed(started_at)
        return 0

    action_label = "run in the sandbox without LLM feedback" if args.action == "run" else "submit for LLM feedback"
    print(
        f"This will {action_label} for {args.count} code actions at {args.api_url}. "
        f"It creates or reuses {min(args.students, args.count)} student accounts."
    )
    if args.action == "submit":
        print(f"Feedback backend: {args.backend!r}. Azure submissions may incur model usage costs.")
    if not args.yes and input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled; no accounts or submissions were created.")
        print_elapsed(started_at)
        return 0

    try:
        student_count = min(args.students, args.count)
        width = max(2, len(str(student_count)))
        tokens = [""] * student_count
        account_setup_started = time.monotonic()
        account_workers = min(args.concurrency, student_count, 8)
        with ThreadPoolExecutor(max_workers=account_workers) as executor:
            token_futures = {
                executor.submit(
                    student_token,
                    args.api_url,
                    f"{args.user_prefix}{index + 1:0{width}d}",
                    STUDENT_PASSWORD,
                ): index
                for index in range(student_count)
            }
            for future in as_completed(token_futures):
                tokens[token_futures[future]] = future.result()
        print(
            f"Prepared {student_count} student accounts in "
            f"{time.monotonic() - account_setup_started:.1f}s "
            f"(up to {account_workers} at a time)."
        )

        status, assignment_rows = api_request(
            args.api_url, "/api/assignments/", token=tokens[0]
        )
        if status != 200 or not isinstance(assignment_rows, list):
            raise RuntimeError(f"Could not list assignments (HTTP {status}): {assignment_rows}")
        available = {row.get("title"): row.get("id") for row in assignment_rows}
        missing = [example.title for example in EXAMPLES if example.title not in available]
        if missing:
            raise RuntimeError(
                "The API is missing seeded assignments: " + ", ".join(missing)
                + ". Start the API with sample assignments enabled before running this script."
            )

        student_jobs: list[list[tuple[Example, str, str]]] = [[] for _ in tokens]
        for index, job in enumerate(planned_jobs):
            student_jobs[index % student_count].append(job)

        print_lock = threading.Lock()

        def run_student(index: int) -> list[tuple[str, str]]:
            token = tokens[index]
            outcomes: list[tuple[str, str]] = []
            for example, category, source in student_jobs[index]:
                endpoint = "/api/submissions/run" if args.action == "run" else "/api/submissions/"
                body = {
                    "assignmentId": available[example.title],
                    "sourceCode": source,
                    "llmBackend": args.backend,
                }
                if args.action == "run":
                    body["standardInput"] = example.run_stdin
                else:
                    body["action"] = "submit"
                status_code, accepted = api_request(
                    args.api_url,
                    endpoint,
                    "POST",
                    token=token,
                    body=body,
                )
                if status_code != 202 or not isinstance(accepted, dict):
                    raise RuntimeError(
                        f"Submission rejected for {example.title} ({category}, HTTP {status_code}): {accepted}"
                    )

                with print_lock:
                    print(
                        f"{index + 1:02d}: queued {example.title} [{category}] "
                        f"(submission {accepted['submissionId']})"
                    )

                result = poll_submission(
                    args.api_url,
                    token,
                    accepted["submissionId"],
                    args.poll_seconds,
                    args.timeout_seconds,
                )
                final_status = result["status"]
                outcomes.append((category, final_status))
                with print_lock:
                    if args.action == "run":
                        print(f"{index + 1:02d}: finished {example.title} [{category}] -> {final_status}")
                        output = result.get("sandboxOutput") or {}
                        compilation = output.get("compilation") or {}
                        if not compilation.get("success", True) and compilation.get("stderr"):
                            print(f"    compile stderr: {compilation['stderr'].strip()}")
                        for run_result in output.get("testResults") or []:
                            if run_result.get("stdout"):
                                print(f"    stdout: {run_result['stdout'].rstrip()}")
                            if run_result.get("stderr"):
                                print(f"    stderr: {run_result['stderr'].rstrip()}")
                            if run_result.get("exitCode") is not None:
                                print(f"    exit code: {run_result['exitCode']}")
                    else:
                        solved = result.get("taskSolved")
                        print(
                            f"{index + 1:02d}: finished {example.title} [{category}] "
                            f"-> {final_status}, taskSolved={solved}"
                        )
                    if final_status == "FAILED":
                        print(f"    error: {result.get('errorMessage') or 'No error detail returned.'}")
            return outcomes

        outcomes: list[tuple[str, str]] = []
        submission_started = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=min(args.concurrency, student_count)
        ) as executor:
            futures = [executor.submit(run_student, index) for index in range(student_count)]
            for future in as_completed(futures):
                outcomes.extend(future.result())

        print(f"Submission processing time: {time.monotonic() - submission_started:.1f}s.")
        print(f"Finished {len(outcomes)} submissions.")
        for category, _ in CATEGORY_RATIOS:
            category_outcomes = [status for kind, status in outcomes if kind == category]
            completed = sum(status == "COMPLETED" for status in category_outcomes)
            failed = sum(status == "FAILED" for status in category_outcomes)
            print(f"{category}: completed={completed}, failed={failed}")
        print_elapsed(started_at)
        return 1 if any(status == "FAILED" for _, status in outcomes) else 0
    except (RuntimeError, TimeoutError, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            print("Interrupted; already accepted submissions will continue in the service.", file=sys.stderr)
        else:
            print(f"Error: {error}", file=sys.stderr)
        print_elapsed(started_at)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
