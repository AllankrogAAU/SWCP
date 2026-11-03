from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from apps.benchmark.artifacts import JsonlWriter, percentile


class ArtifactTests(unittest.TestCase):
    def test_jsonl_writer_flushes_valid_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "results.jsonl"
            with JsonlWriter(path) as writer:
                writer.write({"record_type": "submission", "submission_id": "test"})
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual("test", records[0]["submission_id"])

    def test_percentile_interpolates(self) -> None:
        self.assertEqual(2.5, percentile([1, 2, 3, 4], 50))
        self.assertIsNone(percentile([], 95))


if __name__ == "__main__":
    unittest.main()
