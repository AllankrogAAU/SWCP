from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from apps.benchmark.runner import _load_resume_state
from apps.benchmark.telemetry import TelemetryCollector


def _gpu(model: str, timestamp: str) -> dict[str, object]:
    return {
        "record_type": "gpu_sample", "model_key": model, "timestamp_utc": timestamp, "gpu_index": "0",
        "gpu_name": "GPU", "memory_used_mb": "1000", "memory_total_mb": "16000",
        "utilization_percent": "50", "power_watts": "100.0",
    }


class ResumeTests(unittest.TestCase):
    def test_drops_partial_models_and_rebuilds_missing_model_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            records = [
                {"record_type": "submission", "submission_id": "a1", "model_key": "done-a"},
                {"record_type": "submission", "submission_id": "a2", "model_key": "done-a"},
                {"record_type": "submission", "submission_id": "b1", "model_key": "done-b"},
                {"record_type": "submission", "submission_id": "b2", "model_key": "done-b"},
                _gpu("done-b", "2026-11-09T10:00:00+00:00"),
                _gpu("done-b", "2026-11-09T10:10:00+00:00"),
                {"record_type": "submission", "submission_id": "p1", "model_key": "partial"},
                _gpu("partial", "2026-11-09T11:00:00+00:00"),
                {"record_type": "telemetry_update", "submission_id": "p1"},
                {"record_type": "telemetry_update", "submission_id": "a1"},
            ]
            (run_dir / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
            (run_dir / "run_metadata.json").write_text(json.dumps({"model_runs": [{"model_key": "done-a"}]}))
            telemetry = MagicMock()
            telemetry.collect_prometheus.return_value = {"queries": {}}

            completed, model_runs = _load_resume_state(run_dir, 2, telemetry)

            self.assertEqual({"done-a", "done-b"}, completed)
            rebuilt = next(run for run in model_runs if run["model_key"] == "done-b")
            self.assertTrue(rebuilt["reconstructed_on_resume"])
            self.assertEqual(600, rebuilt["duration_seconds"])
            self.assertEqual(2, rebuilt["gpu"]["sample_count"])
            kept = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
            self.assertFalse(any(r.get("model_key") == "partial" for r in kept))
            self.assertFalse(any(r.get("submission_id") == "p1" for r in kept))
            self.assertTrue(any(r.get("submission_id") == "a1" and r["record_type"] == "telemetry_update"
                                for r in kept))


class TelemetryTests(unittest.TestCase):
    def test_connection_reset_is_recorded_instead_of_raised(self) -> None:
        collector = TelemetryCollector.__new__(TelemetryCollector)
        collector.prometheus_url = "http://prometheus.invalid"
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError(104, "reset")):
            result = collector.collect_prometheus(0.0, 60.0)
        self.assertTrue(all(query["error"] for query in result["queries"].values()))


if __name__ == "__main__":
    unittest.main()
