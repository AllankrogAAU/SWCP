"""Teacher-authorized provisioning of CodeNet and SWCP fixture assignments."""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from typing import Any

import httpx

from apps.benchmark.dataset import CodeNetSample

LOGGER = logging.getLogger(__name__)

FIXTURE_TITLE = "[SWCP benchmark] Pipeline fixtures"
FIXTURE_DESCRIPTION = (
    "Benchmark-only C fixture assignment. Passing code prints the expected prime count; "
    "other fixtures deliberately exercise compilation, runtime, and timeout feedback."
)
FIXTURE_CASES = [{
    "stdin": "",
    "expectedStdout": "Computed primes count: 46\n",
    "timeoutMilliseconds": 2_000,
    "memoryLimitMegabytes": 128,
}]


def _jwt_expiry(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except (IndexError, KeyError, ValueError, TypeError):
        return None


def resolve_teacher_token(base_url: str, min_validity_seconds: float = 600.0) -> str:
    """Return ``BENCHMARK_TEACHER_TOKEN`` while it is valid; otherwise log in as the dev teacher.

    The fallback uses ``DEV_TEACHER_USERNAME``/``DEV_TEACHER_EMAIL`` and ``DEV_TEACHER_PASSWORD``
    (the seeded development teacher), so long unattended runs do not fail on an expired JWT.
    """
    token = os.getenv("BENCHMARK_TEACHER_TOKEN")
    expiry = _jwt_expiry(token) if token else None
    if token and (expiry is None or expiry - time.time() > min_validity_seconds):
        return token
    username = os.getenv("DEV_TEACHER_USERNAME") or os.getenv("DEV_TEACHER_EMAIL")
    password = os.getenv("DEV_TEACHER_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "BENCHMARK_TEACHER_TOKEN is missing or expired; set a fresh teacher token or "
            "DEV_TEACHER_USERNAME/DEV_TEACHER_EMAIL and DEV_TEACHER_PASSWORD."
        )
    response = httpx.post(
        f"{base_url.rstrip('/')}/api/auth/login",
        json={"username": username, "password": password},
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json()
    fresh = data.get("accessToken") or data.get("token")
    if not fresh:
        raise RuntimeError("Teacher login succeeded but returned no access token.")
    LOGGER.info("BENCHMARK_TEACHER_TOKEN missing or expired; obtained a fresh teacher token by login.")
    return str(fresh)


class AssignmentProvisioner:
    def __init__(self, base_url: str, teacher_token: str | None = None) -> None:
        token = teacher_token or os.getenv("BENCHMARK_TEACHER_TOKEN")
        if not token:
            raise RuntimeError(
                "Set BENCHMARK_TEACHER_TOKEN to an authorized teacher bearer token "
                "before provisioning benchmark assignments."
            )
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def ensure_assignments(
        self,
        samples: list[CodeNetSample],
        include_fixtures: bool = True,
    ) -> tuple[dict[str, str], str | None]:
        assignments: dict[str, str] = {}
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30.0) as client:
            response = await client.get("/api/assignments", headers=self._headers)
            response.raise_for_status()
            existing = response.json()
            if not isinstance(existing, list):
                raise RuntimeError("The assignments endpoint returned an unexpected response.")
            by_title = {
                str(item.get("title")): str(item["id"])
                for item in existing
                if isinstance(item, dict) and item.get("title") and item.get("id")
            }

            unique_samples = {sample.problem_id: sample for sample in samples}
            for problem_id, sample in sorted(unique_samples.items()):
                title = f"[SWCP CodeNet GNU11 {problem_id}] {sample.title}"[:240]
                assignment_id = await self._ensure_one(
                    client,
                    by_title,
                    title,
                    sample.description,
                    "Review the C submission against the problem requirements and sample input/output.",
                    [sample.test_case()],
                    "codenet-gnu11",
                )
                assignments[problem_id] = assignment_id

            fixture_id = None
            if include_fixtures:
                fixture_id = await self._ensure_one(
                    client,
                    by_title,
                    FIXTURE_TITLE,
                    FIXTURE_DESCRIPTION,
                    "",
                    FIXTURE_CASES,
                    "standard",
                )
        return assignments, fixture_id

    async def _ensure_one(
        self,
        client: httpx.AsyncClient,
        by_title: dict[str, str],
        title: str,
        description: str,
        task_focus: str,
        test_cases: list[dict[str, Any]],
        compiler_profile: str,
    ) -> str:
        assignment_id = by_title.get(title)
        if assignment_id:
            response = await client.put(
                f"/api/assignments/{assignment_id}/test-cases",
                headers=self._headers,
                json={"testCases": test_cases},
            )
            response.raise_for_status()
            return assignment_id

        response = await client.post(
            "/api/assignments",
            headers=self._headers,
            json={
                "title": title,
                "description": description,
                "taskFocus": task_focus,
                "testCases": test_cases,
                "harnessSource": None,
                "compilerProfile": compiler_profile,
            },
        )
        response.raise_for_status()
        body = response.json()
        assignment_id = body.get("id") if isinstance(body, dict) else None
        if not assignment_id:
            raise RuntimeError(f"Creating assignment '{title}' returned no id.")
        by_title[title] = str(assignment_id)
        return str(assignment_id)
