from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import httpx

from apps.benchmark.artifacts import JsonlWriter
from apps.benchmark.judge import (
    JudgeClient,
    JudgeConfig,
    build_judge_messages,
    detect_solution_leak,
    judge_run,
    localization_scores,
    score_judgement,
)
from apps.benchmark.model_manager import MODELS
from apps.benchmark.preflight import estimate_context_prompt_tokens, estimate_tokens
from apps.benchmark.progress import ProgressTracker
from apps.benchmark.report import generate_report, pareto_front
from apps.benchmark.runner import RunControls, _write_submission_record
from benchmarks.runner import ClientSubmissionResult

ACCEPTED = """#include <stdio.h>
int main(void) {
    int total_value_sum = 0, current_input_value;
    while (scanf("%d", &current_input_value) == 1) total_value_sum += current_input_value;
    printf("%d\\n", total_value_sum);
    return 0;
}
"""
STUDENT = """#include <stdio.h>
int main(void) {
    int s = 0, x;
    while (scanf("%d", &x) == 1) s = x;
    printf("%d\\n", s);
    return 0;
}
"""
JUDGEMENT = {
    "ground_truth_faulty_lines": [4],
    "feedback_identified_lines": [4, 9],
    "root_cause_diagnosis_score": 1.0,
    "reports_nonexistent_bug": False,
    "solution_leak": False,
    "socratic_guidance_score": 4,
    "actionability_clarity_score": 5,
    "socratic_subscores": {"socratic_elicitation": 4, "socratic_scaffolding": 3, "socratic_tone": 5},
    "actionability_subscores": {"jargon_suitability": 5, "next_step_clarity": 4},
    "technical_precision": {"line_precision": 1, "concept_precision": 0},
    "hallucinated_issue_categories": [],
    "checklist": {
        "identifies_error_location": 1,
        "explains_why": 1,
        "zero_solution_leak": 1,
        "encourages_student_reasoning": 1,
        "actionable_next_step": 0,
    },
    "rationale": "ok",
}


class LeakDetectionTests(unittest.TestCase):
    def test_hint_without_code_is_not_a_leak(self) -> None:
        feedback = "Look at line 4: what happens to the running total each time you read a value?"
        self.assertFalse(detect_solution_leak(feedback, ACCEPTED, STUDENT)["leak"])

    def test_fenced_corrected_program_is_a_leak(self) -> None:
        feedback = "Here is the fix:\n```c\n" + ACCEPTED + "```"
        self.assertTrue(detect_solution_leak(feedback, ACCEPTED, STUDENT)["leak"])

    def test_copied_reference_lines_are_a_leak(self) -> None:
        feedback = (
            "Try: int total_value_sum = 0, current_input_value;\n"
            "while (scanf(\"%d\", &current_input_value) == 1) total_value_sum += current_input_value;\n"
            "printf(\"%d\\n\", total_value_sum);"
        )
        self.assertTrue(detect_solution_leak(feedback, ACCEPTED, STUDENT)["leak"])


class ScoringTests(unittest.TestCase):
    def test_localization_uses_line_tolerance(self) -> None:
        self.assertEqual(1.0, localization_scores([4], [5], tolerance=1)["f1"])
        self.assertEqual(0.0, localization_scores([4], [9], tolerance=1)["f1"])
        self.assertIsNone(localization_scores([], [3])["f1"])

    def test_score_judgement_for_faulty_submission(self) -> None:
        metrics = score_judgement("WA", JUDGEMENT, {"leak": False})
        self.assertAlmostEqual(2 * 0.5 * 1.0 / 1.5, metrics["bug_localization_f1"])
        self.assertEqual(1.0, metrics["root_cause_diagnosis_accuracy"])
        self.assertIsNone(metrics["false_positive_hallucination"])
        self.assertEqual(1, metrics["non_spoiler_compliance"])
        self.assertEqual(80.0, metrics["checklist_percent"])

    def test_deterministic_leak_overrides_judge(self) -> None:
        metrics = score_judgement("WA", JUDGEMENT, {"leak": True})
        self.assertEqual(0, metrics["non_spoiler_compliance"])
        self.assertEqual(0, metrics["checklist"]["zero_solution_leak"])
        self.assertEqual(60.0, metrics["checklist_percent"])

    def test_accepted_submission_tracks_hallucination_only(self) -> None:
        metrics = score_judgement("AC", {**JUDGEMENT, "reports_nonexistent_bug": True}, {"leak": False})
        self.assertIsNone(metrics["bug_localization_f1"])
        self.assertIsNone(metrics["root_cause_diagnosis_accuracy"])
        self.assertTrue(metrics["false_positive_hallucination"])
        self.assertIsNone(metrics["line_precision"])

    def test_subscores_are_flattened(self) -> None:
        metrics = score_judgement("WA", JUDGEMENT, {"leak": False})
        self.assertEqual(4, metrics["socratic_elicitation"])
        self.assertEqual(3, metrics["socratic_scaffolding"])
        self.assertEqual(5, metrics["socratic_tone"])
        self.assertEqual(5, metrics["jargon_suitability"])
        self.assertEqual(4, metrics["next_step_clarity"])
        self.assertEqual(1, metrics["line_precision"])
        self.assertEqual(0, metrics["concept_precision"])
        self.assertEqual([], metrics["hallucinated_issue_categories"])

    def test_hallucination_categories_only_kept_for_hallucinations(self) -> None:
        judgement = {**JUDGEMENT, "hallucinated_issue_categories": ["Memory Leak"]}
        self.assertEqual([], score_judgement("AC", judgement, {"leak": False})["hallucinated_issue_categories"])
        judgement["reports_nonexistent_bug"] = True
        self.assertEqual(
            ["Memory Leak"], score_judgement("AC", judgement, {"leak": False})["hallucinated_issue_categories"]
        )

    def test_judge_schema_requires_granular_fields(self) -> None:
        from apps.benchmark.judge import JUDGE_SCHEMA
        for field in ("socratic_subscores", "actionability_subscores", "technical_precision",
                      "hallucinated_issue_categories"):
            self.assertIn(field, JUDGE_SCHEMA["required"])
        self.assertEqual(
            ["socratic_elicitation", "socratic_scaffolding", "socratic_tone"],
            JUDGE_SCHEMA["properties"]["socratic_subscores"]["required"],
        )

    def test_judge_prompt_contains_all_evidence_and_blinds_model(self) -> None:
        messages = build_judge_messages("WA", "Sum the numbers.", STUDENT, ACCEPTED, None, "Check line 4.")
        text = "\n".join(message["content"] for message in messages)
        for fragment in ("Sum the numbers.", "s = x;", "total_value_sum", "Check line 4."):
            self.assertIn(fragment, text)
        self.assertNotIn("qwen", text.lower())


class PreflightAndProgressTests(unittest.TestCase):
    def test_estimate_grows_with_source_and_sandbox_report(self) -> None:
        base = estimate_context_prompt_tokens("desc", STUDENT, None)
        bigger = estimate_context_prompt_tokens("desc", STUDENT * 20, {"compilation": {"success": False,
                                                                                       "stderr": "x" * 3000}})
        self.assertGreater(bigger, base + 1000)
        self.assertEqual(estimate_tokens("é"), 1)

    def test_progress_file_written_every_n_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "LIVE_PROGRESS.md"
            tracker = ProgressTracker(path, 20, ["a", "b"], "run", every=10)
            tracker.set_phase("benchmark", "a")
            for index in range(9):
                tracker.record(True, f"s{index}")
            before = path.read_text(encoding="utf-8")
            tracker.record(False, "s9")
            after = path.read_text(encoding="utf-8")
            self.assertNotIn("10 / 20", before)
            self.assertIn("10 / 20", after)
            self.assertIn("s9", after)


class JudgeClientTests(unittest.TestCase):
    def test_azure_request_shape_and_temperature_fallback(self) -> None:
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            body = json.loads(request.content)
            if "temperature" in body:
                return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature'"}})
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps(JUDGEMENT)}}],
                "usage": {"prompt_tokens": 10},
            })

        async def scenario() -> tuple[dict, JudgeClient]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                client = JudgeClient(JudgeConfig("azure", "https://example.test", "gpt-6-sol", "k"), http)
                result, _ = await client.evaluate([{"role": "user", "content": "x"}])
                return result, client

        result, client = asyncio.run(scenario())
        self.assertEqual(JUDGEMENT, result)
        self.assertEqual(2, len(calls))
        self.assertEqual(
            "/openai/deployments/gpt-6-sol/chat/completions", calls[0].url.path,
        )
        self.assertEqual("k", calls[0].headers["api-key"])
        self.assertEqual(0.0, json.loads(calls[0].content)["temperature"])
        self.assertIsNotNone(client.temperature_note)


def _synthetic_run(run_dir: Path) -> None:
    samples = {}
    with JsonlWriter(run_dir / "results.jsonl") as writer:
        for model_index, model in enumerate(("qwen2.5-coder-7b", "azure-gpt-6-sol")):
            for index, status in enumerate(("CE", "RE", "WA", "AC") * 2):
                codenet_id = f"s{index:09d}"
                samples[codenet_id] = {"description": "d", "source": STUDENT, "accepted_source": ACCEPTED}
                submission_id = f"{model}-{index}"
                writer.write({
                    "record_type": "submission", "submission_id": submission_id, "model_key": model,
                    "context_mode": "with_context_transfer", "traffic_mode": "sequential",
                    "codenet_status": status, "codenet_submission_id": codenet_id, "problem_id": "p00001",
                    "final_status": "COMPLETED", "client_e2e_ms": 4000.0 + 1000 * model_index + index,
                    "frontend_ttft_ms": 900.0 + index, "llm_feedback": "Look at line 4.",
                    "judge": {"status_match": True}, "sandbox_output": None,
                })
                writer.write({
                    "record_type": "telemetry_update", "submission_id": submission_id,
                    "telemetry": {"stage_timings_ms": {"sandbox": 300.0, "llm": 2500.0, "submission_enqueue": 20.0},
                                  "prompt_tokens": 900, "completion_tokens": 300, "tokens_per_second": 60.0},
                })
    (run_dir / "samples.json").write_text(json.dumps(samples), encoding="utf-8")
    (run_dir / "run_metadata.json").write_text(json.dumps({
        "models": ["qwen2.5-coder-7b", "azure-gpt-6-sol"],
        "model_runs": [{"model_key": "qwen2.5-coder-7b", "gpu": {"power_mean_watts": 250.0,
                                                                   "memory_peak_mb": 14000}}],
    }), encoding="utf-8")


class JudgeRunAndReportTests(unittest.TestCase):
    def test_judge_run_then_report_writes_all_figures_and_tables(self) -> None:
        class FakeClient:
            temperature_note = None

            async def evaluate(self, messages):  # noqa: ANN001
                if "verdict for the student code: AC" in messages[1]["content"]:
                    return {
                        **JUDGEMENT,
                        "reports_nonexistent_bug": True,
                        "hallucinated_issue_categories": ["Memory Leak", "Type / Format Warning"],
                    }, {"prompt_tokens": 1}
                return JUDGEMENT, {"prompt_tokens": 1}

            async def aclose(self) -> None:
                pass

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            _synthetic_run(run_dir)
            config = JudgeConfig("azure", "https://example.test", "gpt-6-sol", "secret")
            asyncio.run(judge_run(run_dir, config, client=FakeClient()))
            judgements = [json.loads(line) for line in (run_dir / "judgements.jsonl").read_text().splitlines()]
            self.assertEqual(16, len(judgements))
            self.assertTrue(all(entry["judge_status"] == "ok" for entry in judgements))
            self.assertNotIn("secret", (run_dir / "judge_config.json").read_text())

            report_dir = generate_report(run_dir, {"azure_input_usd_per_mtok": 2.0, "azure_output_usd_per_mtok": 8.0})
            for stem in (
                "fig1_overall_best_model_quality_only", "fig2_technical_metrics_breakdown",
                "fig3_educational_pedagogy_breakdown", "fig4_codenet_status_breakdown",
                "fig5_qos_ttft_and_e2e_latency", "fig6_throughput_cost_and_pareto",
                "fig7_socratic_subcomponents_radar", "fig8_actionability_breakdown_bar",
                "fig9_technical_precision_split", "fig10_hallucination_distribution",
            ):
                for suffix in (".pdf", ".png"):
                    self.assertTrue((report_dir / f"{stem}{suffix}").is_file(), stem + suffix)
            for table in ("table_quality_summary", "table_status_breakdown", "table_qos_summary",
                          "table_granular_subscores", "table_hallucination_categories"):
                self.assertTrue((report_dir / f"{table}.csv").is_file())
                self.assertIn(r"\begin{tabular}", (report_dir / f"{table}.tex").read_text())
            self.assertIn("Memory Leak", (report_dir / "table_hallucination_categories.csv").read_text())
            rows = (report_dir / "table_qos_summary.csv").read_text().splitlines()
            self.assertEqual(3, len(rows))
            azure_cost = 1000 * (900 * 2.0 + 300 * 8.0) / 1e6
            self.assertIn(f"{azure_cost}", rows[2])

    def test_pareto_front(self) -> None:
        self.assertEqual([0, 2], pareto_front([(1.0, 50.0), (2.0, 40.0), (3.0, 90.0)]))


class RunnerRetryTests(unittest.TestCase):
    def test_transient_failures_are_retried_and_recorded(self) -> None:
        results = [
            ClientSubmissionResult("a", "t", "x", "f", 10.0, 1.0, "TIMEOUT", error_message="timeout"),
            ClientSubmissionResult("b", "t", "x", "f", 10.0, 1.0, "COMPLETED", llm_feedback="hint"),
        ]
        runner = MagicMock()

        async def execute(*args, **kwargs):  # noqa: ANN002, ANN003
            return results.pop(0)

        runner.execute_single_submission = execute
        telemetry = MagicMock()
        telemetry.collect_submission.return_value = {"available": False}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "results.jsonl"
            progress = ProgressTracker(Path(temp_dir) / "p.md", 1, ["qwen2.5-coder-7b"], "run")
            with JsonlWriter(path) as writer, patch("apps.benchmark.runner.asyncio.sleep") as sleep:
                sleep.return_value = None
                asyncio.run(_write_submission_record(
                    writer, telemetry, runner, MagicMock(), MODELS["qwen2.5-coder-7b"],
                    "with_context_transfer", STUDENT, "assignment", "step", "codenet_WA", "WA",
                    1000, 64, {"codenet_submission_id": "s1"},
                    RunControls(max_attempts=3, retry_backoff_seconds=0, progress=progress),
                ))
            record = json.loads(path.read_text().splitlines()[0])
            self.assertEqual("b", record["submission_id"])
            self.assertEqual(2, record["attempt_count"])
            self.assertEqual("a", record["failed_attempts"][0]["submission_id"])
            self.assertEqual(1, progress.completed)


if __name__ == "__main__":
    unittest.main()
