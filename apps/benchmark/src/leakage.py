"""Conservative exact-code overlap check for model feedback."""

from __future__ import annotations

import re


_C_TOKEN = re.compile(r"[A-Za-z_]\w*|\d+|==|!=|<=|>=|->|&&|\|\||[{}()[\];,*]")


def leaks_reference(feedback: str, reference: str) -> bool:
    """Flag substantial exact source overlap, not ordinary identifier mentions."""
    if not feedback or not reference:
        return False
    normalized_feedback = " ".join(feedback.split())
    source_lines = [
        " ".join(line.split())
        for line in reference.splitlines()
        if len(" ".join(line.split())) >= 24
        and not line.lstrip().startswith(("//", "/*", "*", "#"))
    ]
    if any(line in normalized_feedback for line in source_lines):
        return True

    source_tokens = _C_TOKEN.findall(_strip_c_comments(reference))
    feedback_tokens = _C_TOKEN.findall(_strip_c_comments(feedback))
    if len(source_tokens) < 24 or len(feedback_tokens) < 24:
        return False
    windows = {
        tuple(source_tokens[index:index + 24])
        for index in range(len(source_tokens) - 23)
    }
    return any(
        tuple(feedback_tokens[index:index + 24]) in windows
        for index in range(len(feedback_tokens) - 23)
    )


def _strip_c_comments(source: str) -> str:
    return re.sub(r"/\*.*?\*/|//[^\r\n]*", " ", source, flags=re.DOTALL)
