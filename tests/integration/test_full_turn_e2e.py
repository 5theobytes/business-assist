"""Opt-in live integration for one full turn with Anthropic and Firestore.

Run only with explicit live-test opt-in and credentials for a dedicated staging
environment; this test can make billable provider requests and write test data.
"""
from __future__ import annotations

import os
import uuid

import pytest

import app.bootstrap  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_anthropic,
    pytest.mark.requires_firestore,
]


TEST_COLLECTION = "sessions_test"


@pytest.fixture
def cleanup():
    ids: list[str] = []
    yield ids
    from google.cloud import firestore
    client = firestore.Client(database=os.environ["FIRESTORE_DATABASE"])
    for sid in ids:
        client.collection(TEST_COLLECTION).document(sid).delete()


def test_full_turn_records_state_and_classifications(monkeypatch, cleanup):
    from app import agent
    from app.session_store import FirestoreStore
    from app.workflows import build
    from app.llm import AnthropicLLM

    llm = AnthropicLLM()
    monkeypatch.setattr(agent, "_llm", llm)
    monkeypatch.setattr(agent, "_workflow", build("v2", llm=llm))
    monkeypatch.setattr(agent, "_store", FirestoreStore(collection=TEST_COLLECTION))
    monkeypatch.setattr(agent, "_warm_cache", {})

    chat_id = 8_000_000 + int(uuid.uuid4().int % 10_000)
    session = agent.create_session(telegram_chat_id=chat_id)
    cleanup.append(session.id)

    # Walk through the deterministic 4-question screening with pure-digit "1"s.
    # No LLM cost here — these are scripted turns.
    for _ in range(4):
        agent.reply(session, "1", telegram_chat_id=chat_id)
    assert session.state.conversation.phase.value != "screening", "screening should be done"

    # Now the real workflow turn — this hits live Claude.
    agent.reply(session, "У меня магазин в телеге, нас трое. А зачем тебе это?",
                telegram_chat_id=chat_id)

    snap = agent._store.client.collection(TEST_COLLECTION).document(session.id).get()
    data = snap.to_dict()
    classifications = data.get("classifications") or []
    # Filter out screening-era classifications (they have rationale starting "screening Q")
    workflow_classifs = [c for c in classifications if not c.get("rationale", "").startswith("screening Q")]
    types = {c["type"] for c in workflow_classifs}

    assert "answer" in types  # split вытащил содержательную часть после screening
    # И часть фактов уехала в state:
    assert (
        session.state.point_a.team_size == 3
        or any("трое" in (p or "") for p in (session.state.point_a.business_type, ""))
        or any("телеграм" in (c or "").lower() for c in session.state.point_a.channels)
    )
