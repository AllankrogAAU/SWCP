from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from apps.benchmark.plots import load_submission_records


class PlotRecordTests(unittest.TestCase):
    def test_telemetry_update_joins_to_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "results.jsonl"
            records = [
                {"record_type": "submission", "submission_id": "submission-1", "telemetry": {"collection_status": "pending"}},
                {"record_type": "telemetry_update", "submission_id": "submission-1", "telemetry": {"available": True}},
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            submissions = load_submission_records(path)

            self.assertEqual([{"available": True}], [record["telemetry"] for record in submissions])

    def test_orphan_telemetry_update_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "results.jsonl"
            path.write_text(
                json.dumps({"record_type": "telemetry_update", "submission_id": "missing"}) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "has no submission record"):
                load_submission_records(path)


if __name__ == "__main__":
    unittest.main()
