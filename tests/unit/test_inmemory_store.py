"""Tests for InMemoryStore — no credentials needed."""
from __future__ import annotations

import pytest

from app.session_store import InMemoryStore, _dict_to_session, _session_to_dict
from app.state import WorkflowState
from app.workflows.base import WorkflowSession


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def sample_session():
    return WorkflowSession(
        id="test-123",
        workflow_name="v2",
        state=WorkflowState(),
        transcript=[{"role": "user", "content": "hello"}],
    )


class TestInMemoryStore:
    def test_save_and_load(self, store, sample_session):
        store.save(sample_session)
        loaded = store.load("test-123")
        assert loaded is not None
        assert loaded.id == "test-123"
        assert loaded.workflow_name == "v2"
        assert len(loaded.transcript) == 1

    def test_load_nonexistent_returns_none(self, store):
        assert store.load("nonexistent") is None

    def test_find_by_telegram_chat(self, store, sample_session):
        store.save(sample_session, telegram_chat_id=42)
        found = store.find_by_telegram_chat(42)
        assert found is not None
        assert found.id == "test-123"

    def test_find_by_telegram_chat_not_found(self, store, sample_session):
        store.save(sample_session, telegram_chat_id=42)
        assert store.find_by_telegram_chat(99) is None

    def test_append_classification(self, store, sample_session):
        store.save(sample_session)
        store.append_classification(
            "test-123", turn=1, kind="noise", text="hi", rationale="greeting"
        )
        assert len(store._classifications["test-123"]) == 1
        entry = store._classifications["test-123"][0]
        assert entry["turn"] == 1
        assert entry["type"] == "noise"

    def test_save_overwrites_existing(self, store, sample_session):
        store.save(sample_session)
        sample_session.transcript.append({"role": "assistant", "content": "hi back"})
        store.save(sample_session)
        loaded = store.load("test-123")
        assert len(loaded.transcript) == 2


class TestSerializationHelpers:
    def test_session_to_dict_includes_all_fields(self):
        session = WorkflowSession(
            id="abc",
            workflow_name="v1",
            transcript=[{"role": "user", "content": "yo"}],
            business_spec="spec text",
            dev_spec="dev text",
        )
        d = _session_to_dict(session, telegram_chat_id=55)
        assert d["workflow_name"] == "v1"
        assert d["business_spec"] == "spec text"
        assert d["dev_spec"] == "dev text"
        assert d["telegram_chat_id"] == 55
        assert "state" in d

    def test_dict_to_session_round_trip(self):
        session = WorkflowSession(
            id="xyz",
            workflow_name="v3",
            transcript=[{"role": "assistant", "content": "hello"}],
        )
        d = _session_to_dict(session, telegram_chat_id=None)
        restored = _dict_to_session("xyz", d)
        assert restored.id == "xyz"
        assert restored.workflow_name == "v3"
        assert len(restored.transcript) == 1

    def test_dict_to_session_handles_empty_data(self):
        restored = _dict_to_session("empty-id", {})
        assert restored.id == "empty-id"
        assert restored.workflow_name == "v2"  # default
        assert restored.transcript == []
