from __future__ import annotations

import unittest

from apps.benchmark.finalize_codenet import _failure_analysis, _metric


class FinalizeCodeNetTests(unittest.TestCase):
    def test_metric_keeps_failed_attempts_but_uses_completed_ttft(self) -> None:
        rows = [
            {
                "final_status": "COMPLETED",
                "frontend_ttft_ms": 100,
                "client_e2e_ms": 300,
                "judge": {"status_match": True},
                "streamed_feedback_matches_final": True,
            },
            {
                "final_status": "FAILED",
                "frontend_ttft_ms": None,
                "client_e2e_ms": 200,
                "judge": {"status_match": False},
                "streamed_feedback_matches_final": None,
            },
        ]

        result = _metric(rows)

        self.assertEqual(2, result["submission_count"])
        self.assertEqual(1, result["completed_count"])
        self.assertEqual(1, result["failed_count"])
        self.assertEqual(1, result["ttft_count"])
        self.assertEqual(100, result["p50_ttft_ms"])
        self.assertEqual(250, result["p50_e2e_ms"])
        self.assertEqual(1, result["status_match_count"])
        self.assertEqual(0.5, result["status_match_rate"])

    def test_failure_analysis_separates_http_errors_from_missing_detail(self) -> None:
        rows = [
            {
                "record_type": "submission",
                "model_key": "qwen2.5-coder-7b",
                "final_status": "FAILED",
                "error_message": "Response status code does not indicate success: 400 (Bad Request).",
                "context_mode": "with_context_transfer",
                "codenet_status": "TLE",
            },
            {
                "record_type": "submission",
                "model_key": "azure-gpt-4o",
                "final_status": "FAILED",
                "error_message": None,
                "context_mode": "raw_code_only",
                "codenet_status": "RE",
            },
        ]

        result = _failure_analysis(rows)

        self.assertEqual(2, result["total_failed_records"])
        self.assertEqual(
            1,
            result["by_model"]["qwen2.5-coder-7b"]["causes"][
                "http_400_bad_request_body_unavailable"
            ],
        )
        self.assertEqual(
            1,
            result["by_model"]["azure-gpt-4o"]["causes"][
                "failed_without_client_error_detail"
            ],
        )


if __name__ == "__main__":
    unittest.main()
