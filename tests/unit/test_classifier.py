"""Tests for app/classifier.py — splits user message into answer/comment."""
from __future__ import annotations

from app.llm import MockLLM


def test_split_returns_both_parts_when_present():
    from app.classifier import split

    def responder(system, messages, tools):
        return {
            "tool_use": {
                "name": "split_user_message",
                "input": {
                    "answer_text": "Нас трое.",
                    "comment_text": "А зачем тебе это?",
                    "rationale": "первое — ответ, второе — мета-вопрос",
                },
            }
        }

    llm = MockLLM(responder=responder)
    result = split(llm, last_assistant="Сколько у вас сотрудников?",
                   last_user="Нас трое. А зачем тебе это?")

    assert result == {
        "answer_text": "Нас трое.",
        "comment_text": "А зачем тебе это?",
        "rationale": "первое — ответ, второе — мета-вопрос",
    }


def test_split_falls_back_to_full_text_when_no_tool_use():
    from app.classifier import split

    def responder(system, messages, tools):
        return {"text": "Извините, я не могу"}

    llm = MockLLM(responder=responder)
    result = split(llm, last_assistant="Q?", last_user="Какой-то ответ")

    assert result["answer_text"] == "Какой-то ответ"
    assert result["comment_text"] is None
    assert "fallback" in result["rationale"]


def test_split_normalises_empty_strings_to_none():
    from app.classifier import split

    def responder(system, messages, tools):
        return {
            "tool_use": {
                "name": "split_user_message",
                "input": {
                    "answer_text": "Нас трое.",
                    "comment_text": "   ",
                    "rationale": "только ответ",
                },
            }
        }

    llm = MockLLM(responder=responder)
    result = split(llm, last_assistant="?", last_user="Нас трое.")

    assert result["answer_text"] == "Нас трое."
    assert result["comment_text"] is None
