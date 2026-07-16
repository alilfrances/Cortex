from __future__ import annotations

import re

_TOKEN_PATTERN = r"'s|'t|'re|'ve|'m|'ll|'d| ?[\p{L}\p{N}]+| ?[^\s\p{L}\p{N}]+|\s+(?!\S)|\s+"

# P1-4: per-kind calibration factors mapping Cortex's stdlib regex-segment
# estimate onto real BPE token counts, meant to be produced by
# evals/calibrate_tokenizer.py against tiktoken's o200k_base encoding over a
# real repo corpus and pasted in here (see that script's docstring and
# evals/RESULTS.md's "Tokenizer calibration" section for the measured table
# once generated). "text" is the default/fallback for kinds the calibration
# script never saw (e.g. the synthetic "commit" kind bundle.py counts).
#
# CAVEAT (see CHANGELOG "Unreleased" / P1-4 entry): these are provisional
# 1.0 (no-op) placeholders, *not* measured values. tiktoken's o200k_base
# vocab file is fetched from openaipublic.blob.core.windows.net on first
# use, and that host was unreachable in the sandbox this change was
# authored in (egress policy blocks it, and the harness's security review
# separately declined a hash-verified third-party mirror of the same file
# as a source of truth for production constants -- correctly, since a
# calibration script whose *point* is producing trustworthy constants
# should not itself depend on a bypass). 1.0 keeps the heuristic path
# byte-for-byte identical to pre-P1-4 behavior (safe: zero regression risk)
# while the tiktoken-exact path (see count_text_tokens) is fully wired and
# will be used automatically -- and be perfectly accurate -- in any normal
# (non-sandboxed) environment with the `[tokens]` extra installed. Before
# treating P1-4 as fully landed, run `pip install tiktoken && python3
# evals/calibrate_tokenizer.py` somewhere with real internet access and
# replace these three numbers with the printed factors.
CALIBRATION: dict[str, float] = {
    "code": 1.0,
    "markdown": 1.0,
    "text": 1.0,
}
_DEFAULT_KIND = "text"

_tiktoken_encoding = None
_tiktoken_unavailable = False


def _fallback_segments(text: str) -> list[str]:
    if not text:
        return []
    return re.findall(r"[A-Za-z0-9_]+|[^\w\s]|\s+", text, flags=re.UNICODE)


def raw_segment_count(text: str) -> int:
    """Uncalibrated regex-segment estimate (i.e. the pre-P1-4 heuristic).

    Prefers the third-party `regex` module (Unicode `\\p{L}`/`\\p{N}` classes)
    when installed, else falls back to a byte-safe stdlib `re` pattern.
    Exposed (not underscored) because it is also the basis
    evals/calibrate_tokenizer.py measures against tiktoken to derive
    CALIBRATION -- it must stay decoupled from CALIBRATION itself.
    """
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


def _get_tiktoken_encoding():
    """Return a cached o200k_base encoder, or None if `tiktoken` is unavailable.

    Soft-import mirrors the existing `regex` pattern above: caches both the
    encoder (success) and the unavailability (failure) so we never retry a
    failed import on every call.
    """
    global _tiktoken_encoding, _tiktoken_unavailable
    if _tiktoken_unavailable:
        return None
    if _tiktoken_encoding is not None:
        return _tiktoken_encoding
    try:
        import tiktoken  # type: ignore
    except Exception:
        _tiktoken_unavailable = True
        return None
    try:
        _tiktoken_encoding = tiktoken.get_encoding("o200k_base")
    except Exception:
        _tiktoken_unavailable = True
        return None
    return _tiktoken_encoding


def count_text_tokens(text: str, kind: str = _DEFAULT_KIND) -> int:
    """Return a deterministic token estimate with byte-safe fallback behavior.

    When the optional `tiktoken` extra ([tokens]) is installed, returns an
    exact o200k_base BPE count (`kind` is irrelevant to that path -- the real
    tokenizer needs no per-kind calibration). Otherwise falls back to the
    stdlib regex-segment heuristic scaled by CALIBRATION[kind] ("code",
    "markdown", or "text"; unknown kinds -- e.g. "commit" -- use the "text"
    factor). Default install stays stdlib-only either way.
    """
    if not text:
        return 0
    encoding = _get_tiktoken_encoding()
    if encoding is not None:
        try:
            # disallowed_special=() treats any "<|...|>"-shaped literal in
            # source/prose as ordinary text instead of raising, since Cortex
            # counts arbitrary repo content, not chat-formatted prompts.
            return len(encoding.encode(text, disallowed_special=()))
        except Exception:
            pass
    factor = CALIBRATION.get(kind, CALIBRATION[_DEFAULT_KIND])
    return max(1, round(raw_segment_count(text) * factor))


def _max_prefix_within_budget(segments: list[str], limit: int, kind: str) -> str:
    """Binary-search the longest joined prefix of `segments` whose
    count_text_tokens(..., kind) fits within `limit`."""
    if limit <= 0:
        return ""
    lo, hi, best = 0, len(segments), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = "".join(segments[:mid]).rstrip()
        if count_text_tokens(candidate, kind) <= limit:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return "".join(segments[:best]).rstrip()


def truncate_text_to_budget(text: str, budget: int, kind: str = _DEFAULT_KIND) -> str:
    """Trim text so count_text_tokens(result, kind) <= budget, byte-safe.

    Binary-searches the cut point against the *same* estimator used to
    compute `budget` in the first place (tiktoken when available, else the
    calibrated heuristic) rather than slicing a fixed proportion of raw
    segments: a naive proportional cut would either overshoot the real
    budget (when the estimator undercounts relative to raw segments) or
    under-fill it (when it overcounts), reintroducing the same bias P1-4
    calibrates away from count_text_tokens itself.
    """
    if budget <= 0 or not text:
        return ""
    if count_text_tokens(text, kind) <= budget:
        return text

    marker = "\n...[truncated]"
    marker_tokens = count_text_tokens(marker, kind)
    content_budget = max(0, budget - marker_tokens)

    segments = _fallback_segments(text)
    trimmed = _max_prefix_within_budget(segments, content_budget, kind)
    if not trimmed:
        return _max_prefix_within_budget(segments, budget, kind)
    return f"{trimmed}{marker}"
