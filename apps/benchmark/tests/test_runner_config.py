"""Configuration validation for the benchmark runner."""

from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import run_benchmark
from run_benchmark import ModelSpec, _load_pricing, _run_key, _weights_complete, parse_model


class RunnerConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(__file__).resolve().parent / f".runner-test-{uuid.uuid4().hex}"
        self.work.mkdir()
        self.addCleanup(shutil.rmtree, self.work)

    def test_model_selectors(self) -> None:
        model = parse_model("azure:Gpt4o")
        self.assertEqual(model, ModelSpec("Azure", "Gpt4o"))
        self.assertEqual(model.frontend_backend, "azure")
        local = parse_model("local:Qwen2.5-Coder-3B-Instruct-AWQ")
        self.assertEqual(local.provider, "vLLM")
        self.assertEqual(local.served_name, "qwen2.5-coder-3b-instruct-awq")

    def test_rejects_unknown_and_escaping_local_model_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Azure"):
            parse_model("azure:unknown")
        with self.assertRaisesRegex(ValueError, "Invalid local model path"):
            parse_model("local:../outside")

    def test_run_key_tracks_source_model_and_context(self) -> None:
        sample = {"id": "s1", "source": "int main(void) {}", "problem_description": "d"}
        first = ModelSpec("vLLM", "a", "a")
        second = ModelSpec("vLLM", "a", "b")
        self.assertNotEqual(_run_key(sample, first), _run_key(sample, second))
        self.assertNotEqual(_run_key(sample, first), _run_key({**sample, "source": "x"}, first))
        self.assertNotEqual(_run_key(sample, first), _run_key(sample, first, "burst_n5"))

    def test_weights_complete_requires_all_shards(self) -> None:
        (self.work / "config.json").write_text("{}", encoding="utf-8")
        self.assertFalse(_weights_complete(self.work))
        (self.work / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"a": "one.safetensors", "b": "two.safetensors"}}), encoding="utf-8"
        )
        (self.work / "one.safetensors").write_bytes(b"x")
        self.assertFalse(_weights_complete(self.work))
        (self.work / "two.safetensors").write_bytes(b"x")
        self.assertTrue(_weights_complete(self.work))

    def test_vllm_settings_detect_awq(self) -> None:
        model_dir = self.work / "m"
        model_dir.mkdir()
        (model_dir / "config.json").write_text(
            json.dumps({"quantization_config": {"quant_method": "awq"}}), encoding="utf-8"
        )
        (model_dir / "model.safetensors").write_bytes(b"x")
        original = run_benchmark.MODEL_ROOT
        run_benchmark.MODEL_ROOT = self.work
        self.addCleanup(setattr, run_benchmark, "MODEL_ROOT", original)
        settings = run_benchmark._vllm_settings(ModelSpec("vLLM", "m", "m"))
        self.assertEqual(settings["VLLM_QUANTIZATION"], "awq_marlin")
        self.assertEqual(settings["VLLM_ENFORCE_EAGER"], "false")
        self.assertLessEqual(float(settings["VLLM_GPU_MEMORY_UTILIZATION"]), 0.92)
        self.assertEqual(settings["VLLM_MAX_MODEL_LEN"], "4096")

    def test_pricing_file_skips_notes(self) -> None:
        path = self.work / "pricing.json"
        path.write_text(
            '{"_note":"x","Azure:Gpt4o":{"input_usd_per_million":2.5,"output_usd_per_million":10}}',
            encoding="utf-8",
        )
        self.assertEqual(
            _load_pricing(path),
            {"Azure:Gpt4o": {"input_usd_per_million": 2.5, "output_usd_per_million": 10.0}},
        )


if __name__ == "__main__":
    unittest.main()
