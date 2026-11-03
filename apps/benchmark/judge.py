"""CodeNet sandbox-status agreement and LLM-as-a-judge rubric scoring of tutor feedback.

The deterministic part (``observed_status``/``judge_sample``) compares CodeNet labels with
SWCP sandbox observations. The rubric part sends each recorded feedback, blinded to the
model that produced it, to an OpenAI-compatible or Azure OpenAI judge deployment
(GPT-6-sol by default) with ``temperature=0.0`` and a strict JSON schema.

Run from the repository root:

    python -m apps.benchmark.judge benchmarks/results/<run-id>
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass, field
import difflib
import json
import logging
import os
from pathlib import Path
import random
import re
import subprocess
from typing import Any

import httpx

LOGGER = logging.getLogger("apps.benchmark.judge")
from apps.benchmark.dataset import RUBRIC_STATUSES  # noqa: E402  (re-exported)
FAULTY_STATUSES = ("CE", "RE", "WA")


def observed_status(
    sandbox_output: dict[str, Any] | None,
    timeout_ms: int,
    memory_limit_mb: int,
) -> str:
    if not sandbox_output:
        return "UNKNOWN"

    classification = str(sandbox_output.get("errorClassification") or "").lower()
    if classification.startswith("worker-error:") or classification == "compile_timeout":
        return "UNKNOWN"
    if "compile" in classification:
        return "CE"
    if "timeout" in classification or "time limit" in classification:
        return "TLE"
    if "memory" in classification or "oom" in classification:
        return "MLE"
    if "runtime" in classification or "signal" in classification:
        return "RE"

    compilation = sandbox_output.get("compilation") or {}
    if not compilation.get("success", False):
        return "CE"

    results = sandbox_output.get("testResults")
    if not isinstance(results, list) or not results:
        return "AC"

    for result in results:
        if not isinstance(result, dict):
            continue
        duration = result.get("durationMilliseconds")
        memory = result.get("memoryConsumedMegabytes")
        exit_code = result.get("exitCode")
        if isinstance(duration, (int, float)) and duration >= timeout_ms:
            return "TLE"
        if isinstance(memory, (int, float)) and memory >= memory_limit_mb:
            return "MLE"
        if exit_code not in (0, None):
            return "RE"
        if not result.get("passed", False):
            return "WA"
    return "AC"


def judge_sample(
    expected_status: str,
    sandbox_output: dict[str, Any] | None,
    timeout_ms: int,
    memory_limit_mb: int,
    task_solved: bool | None,
    context_mode: str,
) -> dict[str, Any]:
    observed = observed_status(sandbox_output, timeout_ms, memory_limit_mb)
    expected_verdict = True if observed == "AC" and context_mode == "with_context_transfer" else None
    verdict_policy_match = task_solved is expected_verdict
    return {
        "expected_status": expected_status,
        "observed_status": observed,
        "status_match": observed == expected_status,
        "task_solved_expected": expected_verdict,
        "task_solved_policy_match": verdict_policy_match,
        "sandbox_available": sandbox_output is not None,
    }


# --------------------------------------------------------------------------------------
# Deterministic solution-leak detection
# --------------------------------------------------------------------------------------

_FENCED_BLOCK = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)
_C_LIKE_LINE = re.compile(
    r"""(
        ;\s*(?://.*)?$                                   # statement terminator
      | ^\s*[{}]\s*$                                     # lone brace
      | ^\s*\#\s*(?:include|define)\b                    # preprocessor
      | \b(?:int|void|char|double|float|long|short|unsigned|size_t)\s+\**\w+\s*\([^)]*\)\s*\{?\s*$
      | ^\s*(?:for|while|if|else\s+if|switch)\s*\(.*\)\s*\{?\s*$
      | ^\s*(?:else|do)\s*\{?\s*$
      | ^\s*return\b.*;
    )""",
    re.MULTILINE | re.VERBOSE,
)
_MAIN_DEFINITION = re.compile(r"\bint\s+main\s*\(")


def _c_like_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() and _C_LIKE_LINE.search(line))


def _normalize_code_line(line: str) -> str:
    return re.sub(r"\s+", "", line)


def detect_solution_leak(
    feedback: str,
    reference_source: str | None = None,
    student_source: str | None = None,
    block_line_threshold: int = 3,
    prose_line_threshold: int = 5,
    reference_overlap_threshold: int = 3,
) -> dict[str, Any]:
    """Flag feedback that contains copy-pasteable corrected C code.

    A leak is any fenced block with at least ``block_line_threshold`` C-like lines, unfenced
    text with at least ``prose_line_threshold`` C-like lines, a ``main`` definition, or at
    least ``reference_overlap_threshold`` non-trivial accepted-reference lines that are not
    already present in the student's own code.
    """
    feedback = feedback or ""
    blocks = _FENCED_BLOCK.findall(feedback)
    block_lines = [_c_like_lines(block) for block in blocks]
    prose = _FENCED_BLOCK.sub("", feedback)
    prose_lines = _c_like_lines(prose)
    has_main = bool(_MAIN_DEFINITION.search(feedback))

    overlap = 0
    if reference_source:
        student = {
            _normalize_code_line(line) for line in (student_source or "").splitlines()
        }
        compact_feedback = _normalize_code_line(feedback)
        for line in reference_source.splitlines():
            normalized = _normalize_code_line(line)
            if len(normalized) >= 12 and normalized not in student and normalized in compact_feedback:
                overlap += 1

    reasons = []
    if any(count >= block_line_threshold for count in block_lines):
        reasons.append("code_block")
    if prose_lines >= prose_line_threshold:
        reasons.append("unfenced_code")
    if has_main:
        reasons.append("main_definition")
    if overlap >= reference_overlap_threshold:
        reasons.append("reference_overlap")
    return {
        "leak": bool(reasons),
        "reasons": reasons,
        "code_block_count": len(blocks),
        "max_block_c_lines": max(block_lines, default=0),
        "unfenced_c_lines": prose_lines,
        "reference_line_overlap": overlap,
    }


# --------------------------------------------------------------------------------------
# Judge prompt, schema and scoring
# --------------------------------------------------------------------------------------

_LIKERT = {"type": "integer", "enum": [1, 2, 3, 4, 5]}
_BINARY = {"type": "integer", "enum": [0, 1]}
JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ground_truth_faulty_lines": {"type": "array", "items": {"type": "integer"}},
        "feedback_identified_lines": {"type": "array", "items": {"type": "integer"}},
        "root_cause_diagnosis_score": {"type": ["number", "null"], "enum": [0, 0.5, 1, None]},
        "reports_nonexistent_bug": {"type": "boolean"},
        "solution_leak": {"type": "boolean"},
        "socratic_guidance_score": _LIKERT,
        "actionability_clarity_score": _LIKERT,
        "checklist": {
            "type": "object",
            "properties": {
                "correct_defect_identification": _BINARY,
                "conceptual_c_explanation": _BINARY,
                "zero_solution_leak": _BINARY,
                "socratic_question_present": _BINARY,
                "novice_appropriate_tone": _BINARY,
            },
            "required": [
                "correct_defect_identification",
                "conceptual_c_explanation",
                "zero_solution_leak",
                "socratic_question_present",
                "novice_appropriate_tone",
            ],
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": [
        "ground_truth_faulty_lines",
        "feedback_identified_lines",
        "root_cause_diagnosis_score",
        "reports_nonexistent_bug",
        "solution_leak",
        "socratic_guidance_score",
        "actionability_clarity_score",
        "checklist",
        "rationale",
    ],
    "additionalProperties": False,
}

JUDGE_SYSTEM_PROMPT = (
    "You are a strict, impartial evaluator of automated tutoring feedback for first- and "
    "second-semester C programming students. You do not know which system wrote the feedback; "
    "judge only the feedback text against the evidence provided. Use the accepted reference "
    "solution and the diff only as ground truth; never reward feedback for length or style. "
    "Return only JSON that matches the schema."
)

_STATUS_MEANING = {
    "AC": "Accepted - the student's program is correct for the problem. It contains NO bug.",
    "CE": "Compile Error - the program fails to compile.",
    "RE": "Runtime Error - the program compiles but crashes or exits abnormally.",
    "WA": "Wrong Answer - the program runs but produces incorrect output.",
}

_RUBRIC = """Scoring instructions:
- ground_truth_faulty_lines: 1-based line numbers of the STUDENT code that cause the CodeNet verdict, determined from the sandbox report, the reference solution and the diff. Use [] for Accepted code.
- feedback_identified_lines: 1-based student line numbers that the feedback points to as faulty (map named variables, expressions or constructs to their lines). Use [] if the feedback points to no specific location.
- root_cause_diagnosis_score: 1 if the feedback correctly explains WHY the defect occurs in C terms, 0.5 if partially correct or vague, 0 if wrong or missing. Use null for Accepted code.
- reports_nonexistent_bug: true if the feedback claims a defect, error or required fix that does not actually exist in the student code (a hallucinated or false-positive bug). Pure style suggestions explicitly framed as optional are not bugs.
- solution_leak: true if the feedback gives a copy-pasteable corrected program, function or multi-line fix that would let the student solve the task without thinking.
- socratic_guidance_score (1-5): how well the feedback probes and prods with conceptual hints and guiding questions instead of dictating the answer (1 = dictates or no guidance, 5 = excellent guiding questions/hints).
- actionability_clarity_score (1-5): whether a 1st/2nd-semester C student can understand the next concrete debugging step without overwhelming jargon (1 = unclear/unactionable, 5 = very clear and actionable).
- checklist (0 or 1 each):
  correct_defect_identification: identifies the actual defect (for Accepted code: correctly indicates the code works / invents no defect);
  conceptual_c_explanation: explains the relevant C concept;
  zero_solution_leak: does not leak a solution;
  socratic_question_present: contains at least one guiding question;
  novice_appropriate_tone: encouraging, novice-appropriate tone and wording.
- rationale: at most three sentences justifying the scores."""


def _numbered(source: str) -> str:
    return "\n".join(f"{index:4d} | {line}" for index, line in enumerate(source.splitlines(), start=1))


def _truncate(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def compact_sandbox_report(sandbox_output: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(sandbox_output, dict):
        return None
    compilation = sandbox_output.get("compilation")
    tests = sandbox_output.get("testResults")
    return {
        "compilation": {
            "success": compilation.get("success"),
            "exitCode": compilation.get("exitCode"),
            "stderr": _truncate(compilation.get("stderr"), 1_500),
        } if isinstance(compilation, dict) else None,
        "errorClassification": sandbox_output.get("errorClassification"),
        "testResults": [
            {
                "passed": test.get("passed"),
                "exitCode": test.get("exitCode"),
                "stdout": _truncate(test.get("stdout") or test.get("actualStdout"), 300),
                "stderr": _truncate(test.get("stderr"), 600),
            }
            for test in tests if isinstance(test, dict)
        ][:3] if isinstance(tests, list) else [],
    }


def build_judge_messages(
    status: str,
    description: str,
    student_source: str,
    reference_source: str,
    sandbox_output: dict[str, Any] | None,
    feedback: str,
) -> list[dict[str, str]]:
    diff = "\n".join(difflib.unified_diff(
        student_source.splitlines(),
        reference_source.splitlines(),
        fromfile="student.c",
        tofile="accepted_reference.c",
        lineterm="",
        n=1,
    ))
    reference_note = (
        "The student code IS an accepted solution; the reference below is the same program."
        if status == "AC" else "A different student's accepted solution to the same problem."
    )
    user = "\n\n".join([
        f"CodeNet verdict for the student code: {status} - {_STATUS_MEANING.get(status, status)}",
        f"Problem description:\n{_truncate(description, 4_000)}",
        f"Student C code (unaltered, with line numbers):\n{_numbered(student_source)}",
        "Deterministic cSandbox report:\n"
        + json.dumps(compact_sandbox_report(sandbox_output), ensure_ascii=False, indent=1),
        f"Accepted reference solution (ground truth). {reference_note}\n```c\n{_truncate(reference_source, 6_000)}\n```",
        f"Unified diff student -> reference:\n{_truncate(diff, 4_000) or '(identical)'}",
        f"Tutor feedback to evaluate (blinded):\n<<<\n{feedback}\n>>>",
        _RUBRIC,
    ])
    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def localization_scores(
    ground_truth: list[int],
    identified: list[int],
    tolerance: int = 1,
) -> dict[str, float | None]:
    truth = set(ground_truth)
    found = set(identified)
    if not truth:
        return {"precision": None, "recall": None, "f1": None}
    if not found:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    precision = sum(any(abs(f - t) <= tolerance for t in truth) for f in found) / len(found)
    recall = sum(any(abs(f - t) <= tolerance for f in found) for t in truth) / len(truth)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


def score_judgement(
    status: str,
    judgement: dict[str, Any],
    leak: dict[str, Any],
    tolerance: int = 1,
) -> dict[str, Any]:
    """Convert a raw judge response into the benchmark metrics (percentages are 0-100)."""
    faulty = status in FAULTY_STATUSES
    localization = (
        localization_scores(
            [int(x) for x in judgement.get("ground_truth_faulty_lines") or []],
            [int(x) for x in judgement.get("feedback_identified_lines") or []],
            tolerance,
        ) if faulty else {"precision": None, "recall": None, "f1": None}
    )
    root_cause = judgement.get("root_cause_diagnosis_score") if faulty else None
    non_spoiler = not bool(judgement.get("solution_leak")) and not bool(leak.get("leak"))
    checklist = dict(judgement.get("checklist") or {})
    # The deterministic detector can only tighten the judge's leak verdict.
    checklist["zero_solution_leak"] = int(bool(checklist.get("zero_solution_leak")) and non_spoiler)
    checklist_points = sum(int(bool(value)) for value in checklist.values())
    return {
        "bug_localization_precision": localization["precision"],
        "bug_localization_recall": localization["recall"],
        "bug_localization_f1": localization["f1"],
        "root_cause_diagnosis_accuracy": float(root_cause) if root_cause is not None else None,
        "false_positive_hallucination": bool(judgement.get("reports_nonexistent_bug")) if status == "AC" else None,
        "reports_nonexistent_bug": bool(judgement.get("reports_nonexistent_bug")),
        "non_spoiler_compliance": int(non_spoiler),
        "judge_solution_leak": bool(judgement.get("solution_leak")),
        "deterministic_solution_leak": bool(leak.get("leak")),
        "socratic_guidance_score": int(judgement.get("socratic_guidance_score")),
        "actionability_clarity_score": int(judgement.get("actionability_clarity_score")),
        "checklist": checklist,
        "checklist_points": checklist_points,
        "checklist_percent": 100.0 * checklist_points / 5,
    }


# --------------------------------------------------------------------------------------
# Judge client (Azure OpenAI or any OpenAI-compatible endpoint, e.g. local vLLM)
# --------------------------------------------------------------------------------------

@dataclass
class JudgeConfig:
    provider: str
    endpoint: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    api_version: str = "2025-04-01-preview"
    temperature: float = 0.0
    max_output_tokens: int = 4_000
    timeout_seconds: float = 180.0
    max_attempts: int = 5

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "model": self.model,
            "api_version": self.api_version if self.provider == "azure" else None,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


def _kubectl(*args: str) -> str:
    result = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def add_judge_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("LLM-as-a-judge")
    group.add_argument("--judge-provider", choices=("azure", "openai"), default=os.getenv("JUDGE_PROVIDER", "azure"))
    group.add_argument("--judge-endpoint", default=os.getenv("JUDGE_ENDPOINT"),
                       help="Azure resource endpoint or OpenAI-compatible base URL ending in /v1.")
    group.add_argument("--judge-model", default=os.getenv("JUDGE_MODEL", "gpt-6-sol"),
                       help="Azure deployment name or OpenAI-compatible model name.")
    group.add_argument("--judge-azure-model-key", default=os.getenv("JUDGE_AZURE_MODEL_KEY", "Gpt6Sol"),
                       help="llm-worker AzureAIFoundry model key used to read the endpoint/key from Kubernetes.")
    group.add_argument("--judge-api-version", default=os.getenv("JUDGE_API_VERSION", "2025-04-01-preview"))
    group.add_argument("--judge-namespace", default=os.getenv("K8S_NAMESPACE", "swcp"))
    group.add_argument("--judge-concurrency", type=int, default=int(os.getenv("JUDGE_CONCURRENCY", "4")))
    group.add_argument("--judge-max-output-tokens", type=int, default=4_000)
    group.add_argument("--judge-line-tolerance", type=int, default=1)


def resolve_judge_config(args: argparse.Namespace) -> JudgeConfig:
    """Resolve judge settings from arguments/env, falling back to the llm-worker's Kubernetes config.

    The API key is read from ``JUDGE_API_KEY`` or the ``llm-worker-secrets`` secret and is never
    written to disk or logs.
    """
    endpoint = args.judge_endpoint
    api_key = os.getenv("JUDGE_API_KEY")
    key = args.judge_azure_model_key
    if args.judge_provider == "azure":
        if not endpoint:
            endpoint = _kubectl(
                "get", "configmap", "llm-worker-config", "-n", args.judge_namespace,
                "-o", f"jsonpath={{.data.AzureAIFoundry__Models__{key}__Endpoint}}",
            )
        if not api_key:
            encoded = _kubectl(
                "get", "secret", "llm-worker-secrets", "-n", args.judge_namespace,
                "-o", f"jsonpath={{.data.AzureAIFoundry__Models__{key}__ApiKey}}",
            )
            api_key = base64.b64decode(encoded).decode("utf-8").strip() if encoded else None
        if not endpoint or not api_key:
            raise RuntimeError(
                f"Azure judge configuration for '{key}' is incomplete; set JUDGE_ENDPOINT/JUDGE_API_KEY "
                "or configure llm-worker-config/llm-worker-secrets."
            )
    elif not endpoint:
        raise RuntimeError("--judge-endpoint (or JUDGE_ENDPOINT) is required for the openai provider.")
    return JudgeConfig(
        provider=args.judge_provider,
        endpoint=endpoint.rstrip("/"),
        model=args.judge_model,
        api_key=api_key,
        api_version=args.judge_api_version,
        max_output_tokens=args.judge_max_output_tokens,
    )


class JudgeClient:
    def __init__(self, config: JudgeConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_seconds)
        self._owns_client = client is None
        self.send_temperature = True
        self.temperature_note: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _request(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, str], dict[str, Any]]:
        body: dict[str, Any] = {
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "feedback_rubric", "schema": JUDGE_SCHEMA, "strict": True},
            },
        }
        if self.send_temperature:
            body["temperature"] = self.config.temperature
        headers = {"Content-Type": "application/json"}
        if self.config.provider == "azure":
            url = (
                f"{self.config.endpoint}/openai/deployments/{self.config.model}/chat/completions"
                f"?api-version={self.config.api_version}"
            )
            headers["api-key"] = self.config.api_key or ""
            body["max_completion_tokens"] = self.config.max_output_tokens
        else:
            url = f"{self.config.endpoint}/chat/completions"
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            body["model"] = self.config.model
            body["max_tokens"] = self.config.max_output_tokens
        return url, headers, body

    async def evaluate(self, messages: list[dict[str, str]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        last_error: str | None = None
        attempt = 0
        while attempt < self.config.max_attempts:
            attempt += 1
            url, headers, body = self._request(messages)
            try:
                response = await self._client.post(url, headers=headers, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"transport: {exc}"
                await asyncio.sleep(min(60, 2 ** attempt))
                continue
            if response.status_code == 400 and "temperature" in body and "temperature" in response.text.lower():
                # Some reasoning deployments only accept their default temperature. Concurrent
                # requests may already be in flight with it, so every such request is retried.
                if self.send_temperature:
                    self.send_temperature = False
                    self.temperature_note = "Deployment rejected an explicit temperature; its default was used."
                    LOGGER.warning(self.temperature_note)
                attempt -= 1
                continue
            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 2 ** (attempt + 1))
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(delay)
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"Judge request failed with HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            content = data["choices"][0]["message"].get("content") or ""
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                last_error = "judge returned invalid JSON"
                continue
            return parsed, data.get("usage")
        raise RuntimeError(f"Judge request failed after {self.config.max_attempts} attempts: {last_error}")


# --------------------------------------------------------------------------------------
# Batch judging of a benchmark run directory
# --------------------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def judgeable_records(run_dir: Path) -> list[dict[str, Any]]:
    from apps.benchmark.plots import load_submission_records

    return [
        record for record in load_submission_records(run_dir / "results.jsonl")
        if record.get("traffic_mode") == "sequential"
        and record.get("codenet_status") in RUBRIC_STATUSES
    ]


async def judge_run(
    run_dir: Path,
    config: JudgeConfig,
    concurrency: int = 4,
    line_tolerance: int = 1,
    progress: Any | None = None,
    client: JudgeClient | None = None,
    seed: int = 2026,
) -> Path:
    from apps.benchmark.artifacts import JsonlWriter

    samples = json.loads((run_dir / "samples.json").read_text(encoding="utf-8"))
    output_path = run_dir / "judgements.jsonl"
    done = {
        record.get("submission_id") for record in _load_jsonl(output_path)
        if record.get("judge_status") != "judge_error"
    }
    pending = [record for record in judgeable_records(run_dir) if record.get("submission_id") not in done]
    random.Random(seed).shuffle(pending)
    if progress is not None:
        progress.judge_total = len(pending)
        progress.set_phase("judging", note=f"Judging {len(pending)} responses with {config.model}")
    judge_client = client or JudgeClient(config)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()

    async def handle(record: dict[str, Any], writer: JsonlWriter) -> None:
        sample = samples.get(str(record.get("codenet_submission_id")))
        status = str(record.get("codenet_status"))
        feedback = record.get("llm_feedback") or record.get("streamed_feedback") or ""
        base = {
            "submission_id": record.get("submission_id"),
            "model_key": record.get("model_key"),
            "context_mode": record.get("context_mode"),
            "codenet_status": status,
            "codenet_submission_id": record.get("codenet_submission_id"),
            "problem_id": record.get("problem_id"),
        }
        if sample is None:
            entry = {**base, "judge_status": "missing_sample"}
        elif record.get("final_status") != "COMPLETED" or not feedback.strip():
            entry = {**base, "judge_status": "no_feedback"}
        else:
            leak = detect_solution_leak(feedback, sample["accepted_source"], sample["source"])
            messages = build_judge_messages(
                status, sample["description"], sample["source"], sample["accepted_source"],
                record.get("sandbox_output"), feedback,
            )
            async with semaphore:
                try:
                    raw, usage = await judge_client.evaluate(messages)
                    entry = {
                        **base,
                        "judge_status": "ok",
                        "judge_raw": raw,
                        "judge_usage": usage,
                        "deterministic_leak": leak,
                        "metrics": score_judgement(status, raw, leak, line_tolerance),
                    }
                except Exception as exc:  # noqa: BLE001 - recorded, run continues
                    LOGGER.error("Judging %s failed: %s", record.get("submission_id"), exc)
                    entry = {**base, "judge_status": "judge_error", "error": str(exc)[:500]}
        async with lock:
            writer.write(entry)
            if progress is not None:
                progress.record_judgement()

    try:
        with JsonlWriter(output_path) as writer:
            await asyncio.gather(*(handle(record, writer) for record in pending))
    finally:
        if client is None:
            await judge_client.aclose()
    (run_dir / "judge_config.json").write_text(json.dumps({
        **config.describe(),
        "temperature_note": judge_client.temperature_note,
        "line_tolerance": line_tolerance,
        "schema": JUDGE_SCHEMA,
        "system_prompt": JUDGE_SYSTEM_PROMPT,
    }, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path)
    add_judge_arguments(parser)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = resolve_judge_config(args)
    output = asyncio.run(judge_run(
        args.run_dir, config, args.judge_concurrency, args.judge_line_tolerance,
    ))
    print(output)


if __name__ == "__main__":
    main()
