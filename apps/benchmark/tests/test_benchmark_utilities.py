"""Unit coverage for benchmark telemetry, judging, calibration, and plotting helpers."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from calibration_core import cohen_kappa, weighted_kappa
from judge_evaluator import _sandbox_summary
from leakage import leaks_reference
from pipeline_metrics import parse_pipeline_logs, summarize_submission
import plot_results


def row(model: str, provider: str, status: str, score: float, latency: int, sample: str, **extra) -> dict:
    return {
        "run_key": f"{model}-{sample}", "model_name": model, "provider": provider, "sample_id": sample,
        "context_mode": "frontend_submit", "status": "COMPLETED", "codenet_status": status,
        "overall_score": score, "e2e_total_latency_ms": latency, "frontend_network_ms": 50,
        "frontend_ttft_ms": 100, "tokens_per_sec": 30.0, "estimated_cost_usd": 0.001 if provider == "Azure" else 0.0,
        "exceeded_15s_sla": latency > 15000, "leaked_solution_code": False,
        "detected_defect_correctly": True, "false_positive_on_correct_code": False,
        "pipeline_breakdown_ms": {"sandbox_execution": 400, "llm_inference": latency - 1000}, **extra,
    }


class BenchmarkUtilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(__file__).resolve().parent / f".util-test-{uuid.uuid4().hex}"
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)

    def test_pipeline_events_extract_stage_durations_and_token_usage(self) -> None:
        lines = [
            '{"Timestamp":"t","State":{"event_name":"pipeline.stage.started","stage":"llm","submission_id":"s1","queue_wait_ms":23}}',
            '{"Timestamp":"t","State":{"event_name":"pipeline.stage.completed","stage":"submission_enqueue","submission_id":"s1","duration_ms":13,"queue_publish_ms":4}}',
            '{"Timestamp":"t","State":{"event_name":"pipeline.stage.completed","stage":"llm","submission_id":"s1","duration_ms":900,"prompt_tokens":70,"completion_tokens":40}}',
            '{"Timestamp":"t","State":{"event_name":"pipeline.completed","submission_id":"s1","duration_ms":1800}}',
            "not json",
        ]
        summary = summarize_submission(parse_pipeline_logs("\n".join(lines))["s1"])
        self.assertEqual(summary["pipeline_breakdown_ms"]["llm_queue"], 23)
        self.assertEqual(summary["pipeline_breakdown_ms"]["nats_task_publish"], 4)
        self.assertEqual(summary["pipeline_breakdown_ms"]["llm_inference"], 900)
        self.assertEqual(summary["prompt_tokens"], 70)
        self.assertEqual(summary["completion_tokens"], 40)

    def test_solution_overlap_requires_substantial_exact_code(self) -> None:
        reference = """int compute_sum(int values[], int count) {
    int total = 0;
    for (int index = 0; index < count; index++) {
        total += values[index];
    }
    return total;
}"""
        self.assertTrue(leaks_reference(f"Full implementation:\n{reference}", reference))
        self.assertFalse(leaks_reference("Use a loop to accumulate the values.", reference))

    def test_kappas(self) -> None:
        rows = [{"h": "a", "j": "a"}, {"h": "b", "j": "b"}, {"h": "a", "j": "b"}, {"h": "b", "j": "a"}]
        self.assertEqual(cohen_kappa(rows, "h", "j")["cohen_kappa"], 0)
        self.assertAlmostEqual(weighted_kappa([0, 1, 5, 3], [0, 1, 5, 3]), 1.0)
        self.assertLess(weighted_kappa([0, 5, 0, 5], [5, 0, 5, 0]), 0)

    def test_sandbox_summary_is_bounded(self) -> None:
        text = _sandbox_summary({"compilation": {"success": False, "exitCode": 1, "stderr": "x" * 5000}, "testResults": []})
        self.assertIn("compile success=False", text)
        self.assertLess(len(text), 2000)

    def test_plot_results_generates_all_figures_tables_and_winner(self) -> None:
        rows = []
        for index, status in enumerate(("CE", "RE", "WA", "TLE_MLE", "AC")):
            rows.append(row("local-a", "vLLM", status, 60 + index, 4000 + index * 100, f"s{index}", peak_vram_mb=10000))
            rows.append(row("local-b", "vLLM", status, 50, 3000, f"s{index}", peak_vram_mb=9000))
            rows.append(row("Gpt4o", "Azure", status, 80, 9000, f"s{index}"))
        burst = []
        for model, provider in (("local-a", "vLLM"), ("Gpt4o", "Azure")):
            for level in (1, 5):
                for slot in range(level):
                    burst.append({"run_key": f"{model}-{level}-{slot}", "model_name": model, "provider": provider,
                                  "burst_level": level, "status": "COMPLETED", "e2e_total_latency_ms": 2000 * level})
        inference = self.work / "inference.jsonl"
        inference.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        burst_path = self.work / "burst.jsonl"
        burst_path.write_text("".join(json.dumps(r) + "\n" for r in burst), encoding="utf-8")
        results = self.work / "results"
        code = plot_results.main(["--inference", str(inference), "--judged", str(inference),
                                  "--burst", str(burst_path), "--results", str(results)])
        self.assertEqual(code, 0)
        for number in range(1, 7):
            self.assertTrue(any((results / "graphs").glob(f"fig{number}_*.pdf")), number)
        self.assertTrue((results / "tables" / "table1_qos_quality.tex").is_file())
        self.assertTrue((results / "tables" / "table3_hybrid_router.tex").is_file())
        self.assertIn("Winning local model: **local-a (vLLM)**", (results / "summary.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
