# tests/test_enrich.py
from __future__ import annotations

from pathlib import Path

import pytest

from cortex.enrich import _PROMPT_TEMPLATE, enrich_repository, make_provider


def test_enrichment_requires_explicit_code_upload_consent():
    with pytest.raises(RuntimeError, match="allow-code-upload"):
        enrich_repository(Path("."), "claude")


def test_make_provider_raises_on_unknown():
    with pytest.raises(ValueError, match='Unknown provider'):
        make_provider('unknown_llm')


def test_prompt_template_contains_required_fields():
    prompt = _PROMPT_TEMPLATE.format(file_path='src/auth.py', content='def login(): pass')
    assert 'src/auth.py' in prompt
    assert 'INFERRED' in prompt
    assert 'AMBIGUOUS' in prompt
    assert 'confidence_score' in prompt


def test_make_provider_claude_raises_without_key(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    with pytest.raises(RuntimeError, match='ANTHROPIC_API_KEY'):
        make_provider('claude')


def test_make_provider_codex_raises_without_key(monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(RuntimeError, match='OPENAI_API_KEY'):
        make_provider('codex')
