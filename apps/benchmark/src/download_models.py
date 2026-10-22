"""Download the local benchmark model matrix into apps/localLLM/models (resumable)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

MODEL_ROOT = Path(__file__).resolve().parents[2] / "localLLM" / "models"

# Ordered smallest first so early models are ready to benchmark while larger ones download.
MODELS: dict[str, str] = {
    "Qwen2.5-Coder-3B-Instruct-AWQ": "Qwen/Qwen2.5-Coder-3B-Instruct-AWQ",
    "Qwen2.5-Coder-7B-Instruct-AWQ": "Qwen/Qwen2.5-Coder-7B-Instruct-AWQ",
    "DeepSeek-R1-Distill-Qwen-7B-AWQ": "casperhansen/deepseek-r1-distill-qwen-7b-awq",
    "Meta-Llama-3.1-8B-Instruct-AWQ-INT4": "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
    "Mistral-Nemo-Instruct-2407-12B-AWQ": "casperhansen/mistral-nemo-instruct-2407-awq",
    "Qwen2.5-Coder-14B-Instruct-AWQ": "Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
}
ALLOW = ["*.json", "*.safetensors", "*.model", "*.txt", "*.py", "tokenizer*", "*.tiktoken"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*", help=f"Subset of: {', '.join(MODELS)}")
    args = parser.parse_args(argv)
    unknown = [name for name in args.names if name not in MODELS]
    if unknown:
        parser.error(f"Unknown model(s): {', '.join(unknown)}")
    for name in args.names or MODELS:
        target = MODEL_ROOT / name
        print(f"Downloading {MODELS[name]} -> {target}", flush=True)
        snapshot_download(MODELS[name], local_dir=target, allow_patterns=ALLOW)
        print(f"Done {name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
