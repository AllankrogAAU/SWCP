from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.benchmark.model_manager import MODELS, LocalModelManager, ModelSpec


class ModelManagerTests(unittest.TestCase):
    def test_installed_models_are_selectable_with_matching_local_paths(self) -> None:
        expected = {
            "qwen2.5-coder-3b": "Qwen2.5-Coder-3B-Instruct-AWQ",
            "qwen2.5-coder-7b": "Qwen2.5-Coder-7B-Instruct-AWQ",
            "qwen2.5-coder-14b": "Qwen2.5-Coder-14B-Instruct-AWQ",
            "meta-llama-3.1-8b": "Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
            "deepseek-r1-distill-qwen-7b": "deepseek-r1-distill-qwen-7b-awq",
        }

        for key, path in expected.items():
            with self.subTest(model=key):
                self.assertEqual("local", MODELS[key].backend)
                self.assertEqual(path, MODELS[key].vllm_path)

    def test_restore_removes_overrides_to_recover_configmap_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            model_root = Path(temp_dir)
            (model_root / "model").mkdir()
            manager = LocalModelManager(namespace="test", model_root=model_root)
            commands: list[tuple[str, ...]] = []

            def fake_run(*args: str) -> str:
                commands.append(args)
                if args[:3] == ("kubectl", "get", "deployment"):
                    deployment = args[3]
                    return json.dumps({
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [{"name": deployment, "env": []}],
                                },
                            },
                        },
                    })
                if args[:3] == ("kubectl", "get", "configmap"):
                    configmap = args[3]
                    values = (
                        {
                            "LLM_MODEL_NAME": "original-model",
                            "LLM_MODEL_PATH": "original-path",
                        }
                        if configmap == "vllm-config"
                        else {"LocalLlm__ModelName": "original-model"}
                    )
                    return json.dumps({
                        "data": values,
                    })
                return ""

            model = ModelSpec("test-model", "local", "Test model", "test-model", "model")
            with patch.object(manager, "_run", side_effect=fake_run):
                manager.switch_to(model)
                manager.restore()

            restore_command = next(
                command for command in commands
                if command[:3] == ("kubectl", "set", "env")
                and command[3] == "deployment/vllm"
                and "LLM_MODEL_NAME-" in command
            )
            self.assertIn("LLM_MODEL_NAME-", restore_command)
            self.assertIn("LLM_MODEL_PATH-", restore_command)
            restore_worker_command = next(
                command for command in commands
                if command[:3] == ("kubectl", "set", "env")
                and command[3] == "deployment/llm-worker"
                and "LocalLlm__ModelName-" in command
            )
            self.assertIn("LocalLlm__ModelName-", restore_worker_command)


if __name__ == "__main__":
    unittest.main()
