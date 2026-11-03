from __future__ import annotations

import unittest

from apps.benchmark.judge import judge_sample, observed_status


class JudgeTests(unittest.TestCase):
    def test_classifies_compile_failure(self) -> None:
        output = {"compilation": {"success": False}, "testResults": []}
        self.assertEqual("CE", observed_status(output, 2_000, 128))

    def test_does_not_mislabel_compile_timeout_or_worker_failure_as_compile_error(self) -> None:
        compile_timeout = {
            "compilation": {"success": False},
            "errorClassification": "compile_timeout",
        }
        worker_failure = {
            "compilation": {"success": False},
            "errorClassification": "worker-error:sandbox failed",
        }
        self.assertEqual("UNKNOWN", observed_status(compile_timeout, 2_000, 128))
        self.assertEqual("UNKNOWN", observed_status(worker_failure, 2_000, 128))

    def test_classifies_sample_mismatch(self) -> None:
        output = {
            "compilation": {"success": True},
            "testResults": [{
                "passed": False,
                "durationMilliseconds": 10,
                "memoryConsumedMegabytes": 4,
                "exitCode": 0,
            }],
        }
        self.assertEqual("WA", observed_status(output, 2_000, 128))

    def test_raw_code_only_never_expects_solved_verdict(self) -> None:
        output = {"compilation": {"success": True}, "testResults": [{"passed": True, "exitCode": 0}]}
        result = judge_sample("AC", output, 2_000, 128, None, "raw_code_only")
        self.assertTrue(result["status_match"])
        self.assertIsNone(result["task_solved_expected"])
        self.assertTrue(result["task_solved_policy_match"])


if __name__ == "__main__":
    unittest.main()
