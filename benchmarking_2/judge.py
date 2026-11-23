from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import httpx

from .config import Config
from .pipeline import Result

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are an impartial evaluator of educational C programming feedback.
Evaluate each feedback response against the assignment description, teacher focus, student C code, and sandbox output.
Do not use a deterministic code-line heuristic: code mentioned in feedback is not automatically a solution leak.
Return JSON with an evaluations array, one object per input case in the exact same order, containing:
technical_correctness (0-5), diagnosis (0-5), non_hallucination (0-5),
non_spoiler (0-5), socratic_guidance (0-5), actionability (0-5),
educational_tone (0-5), overall (0-100), and concise rationale.

Scoring guidance for non_hallucination (0-5):
- Compare claims against the provided assignment_description, teacher_focus, student_code, and sandbox_output.
- Do NOT penalize a model for referencing requirements that appear in assignment_description or teacher_focus.
- On correct submissions (where compilation succeeded and all tests passed): score 5 if the feedback recognizes that the solution works without inventing false defects; score 0-2 only if it falsely claims compilation failed, tests failed, or invents bugs that contradict the working code.
- On buggy submissions: score 4-5 if the feedback stays grounded in real code lines and sandbox findings without fabricating nonexistent compiler errors, nonexistent identifiers, or contradicted test outcomes."""


def _trim_text(text: str | None, limit: int = 500) -> str | None:
    if not text or len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n...[truncated {len(text)} chars]...\n{text[-tail:]}"


def _compact_sandbox_output(raw: str | None) -> str | None:
    if not raw:
        return raw
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _trim_text(raw, 1200)
    if not isinstance(parsed, dict):
        return _trim_text(raw, 1200)

    comp = parsed.get("compilation")
    if isinstance(comp, dict):
        comp = dict(comp)
        comp["stdout"] = _trim_text(comp.get("stdout"), 800)
        comp["stderr"] = _trim_text(comp.get("stderr"), 800)
        parsed["compilation"] = comp

    tests = parsed.get("testResults")
    if isinstance(tests, list):
        compact_tests = []
        for test in tests:
            if isinstance(test, dict):
                t_copy = dict(test)
                t_copy["stdout"] = _trim_text(t_copy.get("stdout"), 400)
                t_copy["stderr"] = _trim_text(t_copy.get("stderr"), 400)
                compact_tests.append(t_copy)
            else:
                compact_tests.append(test)
        parsed["testResults"] = compact_tests

    return json.dumps(parsed)


class CloudJudge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.endpoint = config.judge_endpoint()
        if not config.judge_api_key:
            raise RuntimeError("Set JUDGE_API_KEY or AZURE_OPENAI_API_KEY for cloud judging.")

    async def _request_batch(
        self,
        client: httpx.AsyncClient,
        cases: list[dict[str, Any]],
        max_retries: int = 15,
    ) -> list[dict[str, Any]]:
        last_exc: Exception | None = None
        saw_rate_limit = False
        for attempt in range(1, max_retries + 1):
            try:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "api-key": self.config.judge_api_key,
                        "Authorization": f"Bearer {self.config.judge_api_key}",
                    },
                    json={
                        "model": self.config.judge_model,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps({"cases": cases})},
                        ],
                    },
                )
                if response.status_code == 429 or response.status_code >= 500:
                    saw_rate_limit = saw_rate_limit or (response.status_code == 429)
                    retry_after = float(response.headers.get("Retry-After", 0) or 0)
                    delay = max(retry_after, min(60.0, 15.0 + 10.0 * attempt)) if response.status_code == 429 else max(retry_after, min(45.0, 2.0 ** min(attempt, 5)))
                    detail = response.text.strip().replace("\n", " ")[:300]
                    logger.warning(
                        "Cloud judge HTTP %d on attempt %d/%d (%s); retrying in %.1fs...",
                        response.status_code,
                        attempt,
                        max_retries,
                        detail,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 400:
                    detail = response.text.strip().replace("\n", " ")[:500]
                    raise RuntimeError(f"Cloud judge HTTP {response.status_code}: {detail}")
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                evaluations = parsed.get("evaluations")
                if not isinstance(evaluations, list) or len(evaluations) != len(cases):
                    raise ValueError(
                        f"Cloud judge returned {len(evaluations) if isinstance(evaluations, list) else 'non-list'} "
                        f"evaluations for batch of {len(cases)} cases."
                    )
                return evaluations
            except RuntimeError:
                raise
            except Exception as exc:
                last_exc = exc
                delay = min(60.0, 20.0 + 10.0 * attempt) if saw_rate_limit else min(45.0, 2.0 ** min(attempt, 5))
                logger.warning(
                    "Cloud judge batch error on attempt %d/%d (%s: %s); retrying in %.1fs...",
                    attempt,
                    max_retries,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                if attempt < max_retries:
                    await asyncio.sleep(delay)

        if len(cases) > 1:
            logger.warning(
                "Falling back to single-item judging for batch of %d cases after batch error: %s",
                len(cases),
                last_exc,
            )
            single_evals: list[dict[str, Any]] = []
            for single_case in cases:
                eval_one = await self._request_batch(client, [single_case], max_retries=6)
                single_evals.extend(eval_one)
            return single_evals

        raise RuntimeError(f"Cloud judge failed after {max_retries} retries: {last_exc}") from last_exc

    async def judge(
        self,
        results: list[Result],
        on_Result_judged: Callable[[Result, dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        cases_with_results = [
            (
                result,
                {
                    "record_id": result.item.record_id,
                    "assignment": result.item.assignment_title,
                    "category": result.item.category,
                    "student_code": result.item.source_code,
                    "standard_input": result.item.standard_input,
                    "sandbox_output": _compact_sandbox_output(result.sandbox_output),
                    "feedback": result.feedback,
                },
            )
            for result in results
        ]
        output: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=180.0) as client:
            for start in range(0, len(cases_with_results), self.config.judge_batch_size):
                batch_pairs = cases_with_results[start : start + self.config.judge_batch_size]
                batch_cases = [pair[1] for pair in batch_pairs]
                evaluations = await self._request_batch(client, batch_cases)
                for (result, _), score in zip(batch_pairs, evaluations):
                    if on_Result_judged is not None:
                        on_Result_judged(result, score)
                output.extend(evaluations)
                await asyncio.sleep(1.0)
        return output

    async def judge_rows(
        self,
        rows: list[dict[str, Any]],
        on_row_judged: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        total_batches = (len(rows) + self.config.judge_batch_size - 1) // self.config.judge_batch_size
        async with httpx.AsyncClient(timeout=180.0) as client:
            for batch_idx, start in enumerate(range(0, len(rows), self.config.judge_batch_size), 1):
                batch = rows[start : start + self.config.judge_batch_size]
                cases = [
                    {
                        "record_id": row["record_id"],
                        "assignment": row["assignment_title"],
                        "assignment_description": row.get("assignment_description") or "",
                        "teacher_focus": row.get("teacher_focus") or "",
                        "category": row["category"],
                        "student_code": row.get("student_code") or "",
                        "standard_input": row.get("standard_input") or "",
                        "sandbox_output": _compact_sandbox_output(row.get("sandbox_output")),
                        "feedback": row.get("feedback"),
                    }
                    for row in batch
                ]
                try:
                    evaluations = await self._request_batch(client, cases)
                except Exception as exc:
                    logger.error(
                        "Skipping judge batch %d/%d after retries exhausted: %s",
                        batch_idx,
                        total_batches,
                        exc,
                    )
                    await asyncio.sleep(15.0)
                    continue
                for row, score in zip(batch, evaluations):
                    if on_row_judged is not None:
                        on_row_judged(row, score)
                output.extend(evaluations)
                if batch_idx % 10 == 0 or batch_idx == total_batches:
                    logger.info("Cloud judge progress: %d/%d batches (%d items judged)", batch_idx, total_batches, len(output))
                await asyncio.sleep(1.0)
        return output
