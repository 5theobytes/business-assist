"""Tests for app/workflows/__init__.py and app/workflows/base.py."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.llm import LLMResponse, MockLLM
from app.workflows import WORKFLOWS, build
from app.workflows.base import Workflow, WorkflowSession


class TestWorkflowBuild:
    def test_build_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown workflow"):
            build("nonexistent", llm=MagicMock())

    def test_build_known_workflows(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        for name in WORKFLOWS:
            wf = build(name, llm=mock_llm)
            assert isinstance(wf, Workflow)

    def test_workflows_registry_nonempty(self):
        assert len(WORKFLOWS) > 0
        assert "v1" in WORKFLOWS
        assert "v2" in WORKFLOWS
        assert "v3" in WORKFLOWS
        assert "v4" in WORKFLOWS


class TestWorkflowSession:
    def test_new_session_has_unique_id(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        s1 = wf.new_session()
        s2 = wf.new_session()
        assert s1.id != s2.id

    def test_session_has_workflow_name(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        session = wf.new_session()
        assert session.workflow_name == "v1"

    def test_session_transcript_starts_empty(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        session = wf.new_session()
        assert session.transcript == []

    def test_append_user_message(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        session = wf.new_session()
        session.append("user", "hello")
        assert len(session.transcript) == 1
        assert session.transcript[0]["role"] == "user"
        assert session.transcript[0]["content"] == "hello"

    def test_append_assistant_runs_postprocess(self):
        """Assistant messages are sanitized through postprocess."""
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        session = wf.new_session()
        # Should not raise — postprocess handles it
        session.append("assistant", "Вот ваш план.")
        assert len(session.transcript) == 1
        assert session.transcript[0]["role"] == "assistant"

    def test_append_with_meta(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        session = wf.new_session()
        session.append("user", "hi", meta={"phase": "discovery"})
        assert session.transcript[0]["meta"]["phase"] == "discovery"

    def test_session_specs_default_none(self):
        mock_llm = MockLLM(responder=lambda s, m, t: "ok")
        wf = build("v1", llm=mock_llm)
        session = wf.new_session()
        assert session.business_spec is None
        assert session.dev_spec is None
        assert session.clarity_doc is None
