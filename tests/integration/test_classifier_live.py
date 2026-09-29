"""Opt-in live integration for classifier.split with Anthropic.

Run only with explicit live-test opt-in and a user-provided provider key; it
may make billable API requests.
"""
from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.requires_anthropic]


def test_split_real_message():
    from app.classifier import split
    from app.llm import AnthropicLLM

    llm = AnthropicLLM()
    result = split(
        llm,
        last_assistant="Сколько у вас сотрудников?",
        last_user="Нас трое. А зачем тебе это?",
    )

    assert result["answer_text"] is not None
    assert "трое" in result["answer_text"].lower() or "трёх" in result["answer_text"].lower() or "3" in result["answer_text"]
    assert result["comment_text"] is not None
    assert "зачем" in result["comment_text"].lower()
