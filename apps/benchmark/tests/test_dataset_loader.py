"""Small, synthetic CodeNet archives only; never open a downloaded dataset."""

from __future__ import annotations

import contextlib
import csv
import io
import json
import shutil
import sys
import tarfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import dataset_loader as loader


def source(lines: int = 15, *, trailing_newline: bool = True) -> str:
    text = "\n".join(["int main(void) {"] + [";"] * (lines - 2) + ["}"])
    return text + ("\n" if trailing_newline else "")


def row(sid: str, status: str = "Accepted", pid: str = "p00001", **extra: str) -> dict[str, str]:
    return {
        "submission_id": sid, "problem_id": pid, "language": "C",
        "filename_ext": "c", "status": status, **extra,
    }


class DatasetLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep generated fixtures inside the project, not a system temp directory.
        self.work = Path(__file__).resolve().parent / f".loader-test-{uuid.uuid4().hex}"
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)

    def archive(
        self,
        rows: list[dict[str, str]],
        bodies: dict[str, str],
        descriptions: dict[str, str] | None = None,
        *,
        reverse: bool = False,
        prefix: str = "",
    ) -> Path:
        members: dict[str, bytes] = {}
        for pid in sorted({item["problem_id"] for item in rows}):
            text = io.StringIO(newline="")
            writer = csv.DictWriter(
                text, fieldnames=["submission_id", "problem_id", "language", "filename_ext", "status"]
            )
            writer.writeheader()
            writer.writerows(item for item in rows if item["problem_id"] == pid)
            members[f"metadata/{pid}.csv"] = text.getvalue().encode()
        for pid, html in (descriptions if descriptions is not None else {"p00001": "<p>Add numbers.</p>"}).items():
            members[f"problem_descriptions/{pid}.html"] = html.encode()
        for path, text in bodies.items():
            members[path] = text.encode()
        path = self.work / f"{uuid.uuid4().hex}.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name in sorted(members, reverse=reverse):
                data = members[name]
                info = tarfile.TarInfo(prefix + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return path

    @staticmethod
    def bodies(rows: list[dict[str, str]]) -> dict[str, str]:
        return {
            f"data/{item['problem_id']}/{item['language']}/{item['submission_id']}.{item['filename_ext']}": source()
            for item in rows
        }

    def test_exact_status_grouping_and_accepted_references(self) -> None:
        rows = [
            row("s1", "Compile Error"), row("s2", "Runtime Error"),
            row("s3", "Wrong Answer"), row("s4", "Time Limit Exceeded"),
            row("s5", "Memory Limit Exceeded"), row("s6"),
            row("s7", "Time Limit Exceeded (other)"),
        ]
        records = loader.load_codenet(self.archive(rows, self.bodies(rows)))
        self.assertEqual(
            [item["status"] for item in records], ["CE", "RE", "WA", "TLE_MLE", "TLE_MLE", "AC"]
        )
        for item in records[:-1]:
            self.assertEqual(item["reference_submission_id"], "s6")
            self.assertEqual(item["reference_source"], source())
        self.assertIsNone(records[-1]["reference_source"])
        self.assertEqual(records[0]["original_status"], "Compile Error")

    def test_selection_is_sorted_order_independent_and_bucket_limited(self) -> None:
        rows = [row("z"), row("a"), row("t", "Time Limit Exceeded"), row("m", "Memory Limit Exceeded")]
        bodies = self.bodies(rows)
        first = loader.load_codenet(self.archive(rows, bodies), 1)
        second = loader.load_codenet(self.archive(rows, bodies, reverse=True, prefix="Project_CodeNet/"), 1)
        self.assertEqual(first, second)
        self.assertEqual([item["submission_id"] for item in first], ["a", "m"])
        self.assertEqual(first[1]["reference_submission_id"], "a")
        self.assertEqual(loader.load_codenet(self.archive(rows, bodies), 0), [])

    def test_default_is_twenty_per_bucket(self) -> None:
        rows = [row(f"s{i:03d}", status) for i, status in enumerate(["Accepted"] * 25)]
        records = loader.load_codenet(self.archive(rows, self.bodies(rows)))
        self.assertEqual(len(records), 20)
        self.assertEqual(records[-1]["submission_id"], "s019")

    def test_inclusive_line_limits_and_newlines(self) -> None:
        rows = [row(name) for name in ["short", "min", "max", "long", "crlf", "cr", "empty"]]
        bodies = {
            "data/p00001/C/short.c": source(14),
            "data/p00001/C/min.c": source(15, trailing_newline=False),
            "data/p00001/C/max.c": source(120),
            "data/p00001/C/long.c": source(121),
            "data/p00001/C/crlf.c": source().replace("\n", "\r\n"),
            "data/p00001/C/cr.c": source().replace("\n", "\r"),
            "data/p00001/C/empty.c": "",
        }
        records = loader.load_codenet(self.archive(rows, bodies))
        self.assertEqual([item["submission_id"] for item in records], ["cr", "crlf", "max", "min"])
        self.assertEqual([item["source_lines"] for item in records], [15, 15, 120, 15])

    def test_missing_members_descriptions_references_and_wrong_language(self) -> None:
        rows = [
            row("accepted"), row("bad", "Wrong Answer"), row("absent"),
            row("cpp", language="C++"), row("extension", filename_ext="cpp"),
            row("unsupported", "Presentation Error"),
            row("no_ref", "Compile Error", "p00002"),
            row("no_description", pid="p00003"), row("empty_description", pid="p00004"),
            row("hidden_description", pid="p00005"),
            row("missing_ref", "Runtime Error", "p00006"),
            row("absent_reference", pid="p00006"),
            row("short_ref", pid="p00007"), row("short_ref_error", "Wrong Answer", "p00007"),
        ]
        bodies = self.bodies(rows)
        del bodies["data/p00001/C/absent.c"]
        del bodies["data/p00006/C/absent_reference.c"]
        bodies["data/p00007/C/short_ref.c"] = source(14)
        descriptions = {
            "p00001": "<p>Problem</p>", "p00002": "Problem", "p00004": " \n ",
            "p00005": "<head>title</head><script>code</script><style>css</style>",
            "p00006": "Problem", "p00007": "Problem",
        }
        records = loader.load_codenet(self.archive(rows, bodies, descriptions))
        self.assertEqual([item["submission_id"] for item in records], ["accepted", "bad"])

    def test_metadata_is_required_and_problem_matching_is_exact(self) -> None:
        rows = [row("accepted")]
        bodies = self.bodies(rows)
        bodies["data/p00001/C/orphan.c"] = source()
        path = self.archive(rows, bodies, {"p000010": "Not the matching problem"})
        self.assertEqual(loader.load_codenet(path), [])
        path = self.archive([], {"data/p00001/C/orphan.c": source()})
        self.assertEqual(loader.load_codenet(path), [])

    def test_html_description_extraction(self) -> None:
        html = (
            "<html><head><title>Ignore title</title></head><body>"
            "<h1>A &amp; B</h1><p>Add <b>two</b> numbers.&nbsp;Now.</p>"
            "<script>ignore()</script><style>.hidden{}</style>"
            "<pre>1\n 2</pre><p>Output<br>3 &lt; 4</p></body></html>"
        )
        self.assertEqual(loader.extract_description(html), "A & B Add two numbers. Now. 1 2 Output 3 < 4")
        rows = [row("accepted")]
        records = loader.load_codenet(self.archive(rows, self.bodies(rows), {"p00001": html}))
        self.assertEqual(records[0]["problem_description"], loader.extract_description(html))

    def test_streaming_and_final_pass_reads_only_selected_bodies(self) -> None:
        rows = [row("a"), row("b"), row("c", "Wrong Answer")]
        path = self.archive(rows, self.bodies(rows))
        real_open = tarfile.open
        opened: list[str] = []
        reads: list[list[str]] = []

        def tracked_open(*args: object, **kwargs: object) -> tarfile.TarFile:
            opened.append(str(kwargs.get("mode")))
            archive = real_open(*args, **kwargs)
            current: list[str] = []
            reads.append(current)
            real_extract = archive.extractfile

            def tracked_extract(member: tarfile.TarInfo) -> object:
                current.append(member.name)
                return real_extract(member)

            archive.extractfile = tracked_extract
            return archive

        with patch.object(loader.tarfile, "open", side_effect=tracked_open), patch.object(
            tarfile.TarFile, "extractall", side_effect=AssertionError("must not extract")
        ), patch.object(tarfile.TarFile, "getmembers", side_effect=AssertionError("must stream")):
            records = loader.load_codenet(path, 1)
        self.assertEqual(opened, ["r|gz"] * 3)
        self.assertEqual(set(reads[2]), {"data/p00001/C/a.c", "data/p00001/C/c.c"})
        self.assertEqual(len(records), 2)

    def test_repo_fixtures_are_separate_unfiltered_and_not_executed(self) -> None:
        records = list(loader.load_repo_fixtures())
        expected = sorted(loader.DEFAULT_FIXTURE_DIR.glob("*.c"))
        self.assertEqual(len(records), len(expected))
        self.assertGreater(len(records), 0)
        self.assertEqual([Path(str(item["source_path"])).name for item in records], [p.name for p in expected])
        for item, path in zip(records, expected):
            self.assertEqual(item["slice"], "repo_validation")
            self.assertIsNone(item["status"])
            self.assertIsNone(item["reference_source"])
            self.assertEqual(item["source"], path.read_text(encoding="utf-8"))
        broken = next(item for item in records if str(item["source_path"]).endswith("broken_syntax.c"))
        self.assertLess(broken["source_lines"], 15)
        with self.assertRaises(FileNotFoundError):
            list(loader.load_repo_fixtures(self.work / "missing"))

    def test_cli_jsonl_stdout_and_file(self) -> None:
        rows = [row("accepted")]
        path = self.archive(rows, self.bodies(rows))
        fixtures = self.work / "fixtures"
        fixtures.mkdir()
        (fixtures / "tiny.c").write_text("int main() {}\n", encoding="utf-8")
        output = io.StringIO()
        args = [str(path), "--samples-per-status", "1", "--fixtures-dir", str(fixtures)]
        with contextlib.redirect_stdout(output):
            self.assertEqual(loader.main(args), 0)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["slice"] for item in records], ["codenet", "repo_validation"])
        destination = self.work / "dataset.jsonl"
        self.assertEqual(loader.main(args + ["--output", str(destination)]), 0)
        self.assertEqual([json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()], records)

    def test_invalid_arguments(self) -> None:
        for kwargs in [
            {"samples_per_status": -1}, {"min_lines": 0},
            {"min_lines": 121, "max_lines": 120},
        ]:
            with self.assertRaises(ValueError):
                loader.load_codenet(self.work / "not-opened.tar.gz", **kwargs)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            loader.main(["not-opened.tar.gz", "--samples-per-status", "-1"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
