from __future__ import annotations

import re

_TOKEN_PATTERN = r"'s|'t|'re|'ve|'m|'ll|'d| ?[\p{L}\p{N}]+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+"


def _fallback_segments(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"[A-Za-z0-9_]+|[^\w\s]|\s+", text, flags=re.UNICODE)


def count_text_tokens(text: str) -> int:
    """Return a deterministic token estimate with byte-safe fallback behavior."""
    if not text:
        return 0
    try:
        import regex  # type: ignore

        matches = regex.findall(_TOKEN_PATTERN, text)
        if matches:
            return len(matches)
    except Exception:
        pass
    return len(_fallback_segments(text))


def truncate_text_to_budget(text: str, budget: int) -> str:
    if budget <= 0 or not text:
        return ""

    segments = _fallback_segments(text)
    if len(segments) <= budget:
        return text

    marker = "\n...[truncated]"
    marker_tokens = len(_fallback_segments(marker))
    content_budget = max(0, budget - marker_tokens)
    trimmed = "".join(segments[:content_budget]).rstrip()
    if not trimmed:
        return "".join(segments[:budget]).rstrip()
    return f"{trimmed}{marker}"
