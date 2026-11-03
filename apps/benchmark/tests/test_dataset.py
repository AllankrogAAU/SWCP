from __future__ import annotations

import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from apps.benchmark.dataset import load_archive_samples, select_samples


PROBLEM_LIST = (
    "id,name,dataset,time_limit,memory_limit,rating,tags,complexity\n"
    "p00001,Sample problem,AIZU,1000,131072,,,\n"
).encode()
SUBMISSIONS = (
    "submission_id,problem_id,user_id,date,original_language,language,ext,status,cpu_time,memory,code_size,accuracy\n"
    "s100000001,p00001,u100000001,1,C,C,c,Accepted,1,100,10,1/1\n"
    "s100000002,p00001,u100000002,2,C,C,c,Wrong Answer,1,100,10,0/1\n"
).encode()
DESCRIPTION = b"<html><style>hidden</style><body><h1>Sample task</h1><p>Return a result.</p></body></html>"
SOURCE_ACCEPTED = b"#include <stdio.h>\nint main(void) { puts(\"ok\"); }\n"
SOURCE_WRONG = b"int main(void) { return 1; }\n"


def _add_member(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    archive.addfile(info, io.BytesIO(content))


class CodeNetDatasetTests(unittest.TestCase):
    def _create_tree(self, root: Path) -> None:
        (root / "metadata").mkdir(parents=True)
        (root / "problem_descriptions").mkdir()
        (root / "derived/input_output/data/p00001").mkdir(parents=True)
        (root / "data/p00001/C").mkdir(parents=True)
        (root / "metadata/problem_list.csv").write_bytes(PROBLEM_LIST)
        (root / "metadata/p00001.csv").write_bytes(SUBMISSIONS)
        (root / "problem_descriptions/p00001.html").write_bytes(DESCRIPTION)
        (root / "derived/input_output/data/p00001/input.txt").write_text("1\n", encoding="utf-8")
        (root / "derived/input_output/data/p00001/output.txt").write_text("ok\n", encoding="utf-8")
        (root / "data/p00001/C/s100000001.c").write_bytes(SOURCE_ACCEPTED)
        (root / "data/p00001/C/s100000002.c").write_bytes(SOURCE_WRONG)

    def test_select_samples_pairs_non_accepted_submission_with_same_problem_ac(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_tree(root)

            samples = select_samples(root, per_status=1, statuses=("AC", "WA"), seed=7)

            self.assertEqual(["AC", "WA"], [sample.status for sample in samples])
            self.assertEqual({"p00001"}, {sample.problem_id for sample in samples})
            wrong_answer = next(sample for sample in samples if sample.status == "WA")
            self.assertEqual("s100000001", wrong_answer.accepted_submission_id)
            self.assertEqual("s100000002", wrong_answer.submission_id)
            self.assertEqual("Sample task Return a result.", wrong_answer.description)
            self.assertEqual("ok\n", wrong_answer.expected_stdout)
            self.assertIn("return 1", wrong_answer.read_source())
            self.assertIn('puts("ok")', wrong_answer.read_accepted_source())

    def test_select_samples_supports_codenet_filename_ext_column(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_tree(root)
            actual_header = SUBMISSIONS.replace(b",ext,status,", b",filename_ext,status,")
            (root / "metadata/p00001.csv").write_bytes(actual_header)

            samples = select_samples(root, per_status=1, statuses=("WA",), seed=7)

            self.assertEqual(1, len(samples))
            self.assertEqual("WA", samples[0].status)

    def test_select_samples_builds_multiple_candidate_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_tree(root)
            with (root / "metadata/p00001.csv").open("a", encoding="utf-8") as file:
                file.write("s100000003,p00001,u100000003,3,C,C,c,Wrong Answer,1,100,10,0/1\n")
            (root / "data/p00001/C/s100000003.c").write_bytes(b"int main(void) { return 2; }\n")

            samples = select_samples(
                root,
                per_status=1,
                statuses=("WA",),
                seed=7,
                candidate_pool_multiplier=3,
            )

            self.assertEqual(2, len(samples))
            self.assertEqual({"s100000002", "s100000003"}, {sample.submission_id for sample in samples})

    def test_archive_loader_extracts_only_selected_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            archive_path = base / "codenet.tar.gz"
            members = [
                ("Project_CodeNet/data/p00001/C/s100000001.c", SOURCE_ACCEPTED),
                ("Project_CodeNet/data/p00001/C/s100000002.c", SOURCE_WRONG),
                ("Project_CodeNet/problem_descriptions/p00001.html", DESCRIPTION),
                ("Project_CodeNet/derived/input_output/data/p00001/input.txt", b"1\n"),
                ("Project_CodeNet/derived/input_output/data/p00001/output.txt", b"ok\n"),
                ("Project_CodeNet/metadata/problem_list.csv", PROBLEM_LIST),
                ("Project_CodeNet/metadata/p00001.csv", SUBMISSIONS),
            ]
            with tarfile.open(archive_path, "w:gz") as archive:
                for name, content in members:
                    _add_member(archive, name, content)

            samples = load_archive_samples(
                archive_path,
                base / "cache",
                per_status=1,
                statuses=("WA",),
                seed=3,
            )

            self.assertEqual(1, len(samples))
            self.assertEqual("WA", samples[0].status)
            self.assertTrue(samples[0].source_path.is_file())
            self.assertTrue(samples[0].accepted_source_path.is_file())
            self.assertFalse((base / "cache/data/p00001/C/unselected.c").exists())

    def test_select_samples_reports_short_status_strata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_tree(root)

            with self.assertRaisesRegex(ValueError, "verified C problem pairs"):
                select_samples(root, per_status=2, statuses=("WA",))

    def test_select_samples_rejects_empty_status_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._create_tree(root)

            with self.assertRaisesRegex(ValueError, "At least one CodeNet status"):
                select_samples(root, statuses=())


if __name__ == "__main__":
    unittest.main()
