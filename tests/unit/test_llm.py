"""Tests for app/llm.py — LLM abstraction layer."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.llm import (
    LLM,
    LLMResponse,
    MockLLM,
    _parse_response,
    _strip_schema_for_gemini,
    get_default_llm,
    to_json,
)


class TestLLMResponse:
    def test_defaults(self):
        r = LLMResponse()
        assert r.text == ""
        assert r.tool_use is None
        assert r.stop_reason == "end_turn"
        assert r.raw is None

    def test_with_values(self):
        r = LLMResponse(text="hello", tool_use={"name": "fn", "input": {}}, stop_reason="tool_use")
        assert r.text == "hello"
        assert r.tool_use == {"name": "fn", "input": {}}


class TestParseResponse:
    def test_text_only(self):
        block = MagicMock()
        block.type = "text"
        block.text = "Hello world"
        resp = MagicMock()
        resp.content = [block]
        resp.stop_reason = "end_turn"

        result = _parse_response(resp)
        assert result.text == "Hello world"
        assert result.tool_use is None

    def test_text_and_tool_use(self):
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Thinking..."

        tool_block = MagicMock()
        tool_block.type = "tool_use"
        tool_block.name = "update_state"
        tool_block.input = {"field": "value"}

        resp = MagicMock()
        resp.content = [text_block, tool_block]
        resp.stop_reason = "tool_use"

        result = _parse_response(resp)
        assert result.text == "Thinking..."
        assert result.tool_use == {"name": "update_state", "input": {"field": "value"}}

    def test_multiple_text_blocks_concatenated(self):
        block1 = MagicMock()
        block1.type = "text"
        block1.text = "Hello "

        block2 = MagicMock()
        block2.type = "text"
        block2.text = "world"

        resp = MagicMock()
        resp.content = [block1, block2]
        resp.stop_reason = "end_turn"

        result = _parse_response(resp)
        assert result.text == "Hello world"


class TestMockLLM:
    def test_string_responder(self):
        llm = MockLLM(responder=lambda s, m, t: "reply")
        result = llm.complete(system="sys", messages=[{"role": "user", "content": "hi"}])
        assert result.text == "reply"
        assert len(llm.calls) == 1

    def test_dict_responder(self):
        llm = MockLLM(
            responder=lambda s, m, t: {"text": "ok", "tool_use": {"name": "fn", "input": {}}}
        )
        result = llm.complete(system="sys", messages=[])
        assert result.text == "ok"
        assert result.tool_use == {"name": "fn", "input": {}}

    def test_llmresponse_responder(self):
        expected = LLMResponse(text="direct", stop_reason="stop")
        llm = MockLLM(responder=lambda s, m, t: expected)
        result = llm.complete(system="sys", messages=[])
        assert result is expected

    def test_tracks_calls(self):
        llm = MockLLM(responder=lambda s, m, t: "x")
        llm.complete(system="s1", messages=[{"role": "user", "content": "a"}])
        llm.complete(system="s2", messages=[{"role": "user", "content": "b"}])
        assert len(llm.calls) == 2
        assert llm.calls[0]["system"] == "s1"
        assert llm.calls[1]["system"] == "s2"


class TestStripSchemaForGemini:
    def test_removes_additional_properties(self):
        schema = {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "additionalProperties": False,
        }
        result = _strip_schema_for_gemini(schema)
        assert "additionalProperties" not in result
        assert result["type"] == "object"

    def test_handles_nested(self):
        schema = {
            "type": "object",
            "properties": {
                "inner": {"type": "object", "$schema": "http://foo", "description": "ok"}
            },
        }
        result = _strip_schema_for_gemini(schema)
        inner = result["properties"]["inner"]
        assert "$schema" not in inner
        assert inner["description"] == "ok"

    def test_handles_none_input(self):
        assert _strip_schema_for_gemini(None) == {}

    def test_handles_list_items(self):
        schema = {
            "type": "array",
            "items": [{"type": "string", "$defs": {}}],
        }
        result = _strip_schema_for_gemini(schema)
        assert "$defs" not in result["items"][0]


class TestGetDefaultLLM:
    def test_raises_when_no_backend_configured(self, monkeypatch):
        monkeypatch.delenv("LLM_BACKEND", raising=False)
        monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="No LLM backend configured"):
            get_default_llm()

    def test_anthropic_backend_without_key_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "anthropic")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY is not set"):
            get_default_llm()

    def test_gemini_backend_without_project_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_BACKEND", "gemini")
        monkeypatch.delenv("VERTEX_PROJECT_ID", raising=False)
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        with pytest.raises(RuntimeError, match="project_id missing"):
            get_default_llm()


class TestToJson:
    def test_serializes_dict(self):
        result = to_json({"key": "value", "num": 42})
        assert '"key": "value"' in result
        assert '"num": 42' in result

    def test_handles_unicode(self):
        result = to_json({"text": "Привет"})
        assert "Привет" in result  # ensure_ascii=False
