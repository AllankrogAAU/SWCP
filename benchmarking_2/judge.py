from __future__ import annotations

import json
from typing import Any

import httpx

from .config import Config
from .dataset import Submission
from .pipeline import Result


SYSTEM_PROMPT = """You are an impartial evaluator of educational programming feedback.
Evaluate each response against the assignment and student code. Do not use a deterministic
code-line heuristic: code mentioned in feedback is not automatically a solution leak.
Return JSON with an evaluations array, one object per input, containing:
technical_correctness (0-5), diagnosis (0-5), non_hallucination (0-5),
non_spoiler (0-5), socratic_guidance (0-5), actionability (0-5),
educational_tone (0-5), overall (0-100), and concise rationale.
Judge whether the feedback reveals a complete solution semantically, not whether it contains code."""


class CloudJudge:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.endpoint = config.judge_endpoint()
        if not config.judge_api_key:
            raise RuntimeError("Set JUDGE_API_KEY or AZURE_OPENAI_API_KEY for cloud judging.")

    async def judge(self, results: list[Result]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=180) as client:
            for start in range(0, len(results), self.config.judge_batch_size):
                batch = results[start:start + self.config.judge_batch_size]
                cases = [
                    {
                        "record_id": result.item.record_id,
                        "assignment": result.item.assignment_title,
                        "category": result.item.category,
                        "student_code": result.item.source_code,
                        "standard_input": result.item.standard_input,
                        "sandbox_output": result.sandbox_output,
                        "feedback": result.feedback,
                    }
                    for result in batch
                ]
                response = await client.post(
                    self.endpoint,
                    headers={"api-key": self.config.judge_api_key, "Authorization": f"Bearer {self.config.judge_api_key}"},
                    json={
                        "model": self.config.judge_model,
                        "response_format": {"type": "json_object"},
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": json.dumps({"cases": cases})},
                        ],
                    },
                )
                if response.status_code >= 400:
                    detail = response.text.strip().replace("\n", " ")[:500]
                    raise RuntimeError(
                        f"Cloud judge HTTP {response.status_code}: {detail}"
                    )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                evaluations = parsed.get("evaluations")
                if not isinstance(evaluations, list) or len(evaluations) != len(batch):
                    raise ValueError("Cloud judge returned an invalid evaluation count.")
                output.extend(evaluations)
        return output

    async def judge_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=180) as client:
            for start in range(0, len(rows), self.config.judge_batch_size):
                batch = rows[start:start + self.config.judge_batch_size]
                cases = [
                    {
                        "record_id": row["record_id"],
                        "assignment": row["assignment_title"],
                        "category": row["category"],
                        "student_code": row.get("student_code") or "",
                        "standard_input": row.get("standard_input") or "",
                        "sandbox_output": row["sandbox_output"],
                        "feedback": row["feedback"],
                    }
                    for row in batch
                ]
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
                if response.status_code >= 400:
                    detail = response.text.strip().replace("\n", " ")[:500]
                    raise RuntimeError(
                        f"Cloud judge HTTP {response.status_code}: {detail}"
                    )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                evaluations = parsed.get("evaluations")
                if not isinstance(evaluations, list) or len(evaluations) != len(batch):
                    raise ValueError("Cloud judge returned an invalid evaluation count.")
                output.extend(evaluations)
        return output
