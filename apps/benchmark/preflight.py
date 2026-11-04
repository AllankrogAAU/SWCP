"""Conservative prompt-length estimates for the with-context-transfer SWCP prompt."""

from __future__ import annotations

import json
import math
from typing import Any

# Covers the ~700-token coreApi system prompt (incl. Socratic boundary rules), teacher focus,
# flow instructions, and chat-template overhead, estimated at the same 3 chars/token rate.
SYSTEM_PROMPT_TOKEN_ALLOWANCE = 1_000
DEFAULT_TOKEN_BUDGET = 2_500


def estimate_tokens(text: str) -> int:
    """Upper-bound style estimate: ~3 ASCII characters per token, one token per other character.

    Real BPE tokenizers (Qwen, Llama, DeepSeek) average 3.5-4 characters per token for C
    source and English text, so this deliberately over-estimates to keep a safety margin.
    """
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return math.ceil(ascii_chars / 3.0) + (len(text) - ascii_chars)


def build_context_user_prompt(
    description: str,
    source: str,
    sandbox_output: dict[str, Any] | None,
) -> str:
    """Mirror of SubmissionPromptBuilder's with_context_transfer user prompt."""
    sandbox_json = json.dumps(sandbox_output or {}, ensure_ascii=False, separators=(",", ":"))
    prompt = (
        f"Assignment:\n{description}\n\nC source:\n```c\n{source}\n```\n\n"
        f"Sandbox output:\n{sandbox_json}"
    )
    compilation = (sandbox_output or {}).get("compilation")
    if isinstance(compilation, dict) and compilation.get("success"):
        diagnostics = [
            str(value).strip()
            for value in (compilation.get("stderr"), (sandbox_output or {}).get("lintOutput"))
            if value and str(value).strip()
        ]
        if diagnostics:
            prompt += "\n\nNon-fatal compiler/lint warnings (compilation succeeded):\n" + "\n".join(diagnostics)
    return prompt


def estimate_context_prompt_tokens(
    description: str,
    source: str,
    sandbox_output: dict[str, Any] | None,
) -> int:
    return SYSTEM_PROMPT_TOKEN_ALLOWANCE + estimate_tokens(
        build_context_user_prompt(description, source, sandbox_output)
    )
