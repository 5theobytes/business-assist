"""End-to-end pipeline tests for app/agent.py with MockLLM + real Firestore."""
from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

import app.bootstrap  # noqa: F401


def _has_firestore_creds() -> bool:
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("GCP_PROJECT_ID")
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_firestore,
    pytest.mark.skipif(
        not _has_firestore_creds(),
        reason="Firestore credentials missing",
    ),
]


TEST_COLLECTION = "sessions_test"


@pytest.fixture
def created_ids():
    ids: list[str] = []
    yield ids
    from google.cloud import firestore
    client = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE") or "(default)")
    for sid in ids:
        client.collection(TEST_COLLECTION).document(sid).delete()


@pytest.fixture
def patched_agent(monkeypatch):
    """Wire agent module with a MockLLM and a Firestore store on sessions_test."""
    from app import agent
    from app.llm import MockLLM
    from app.session_store import FirestoreStore
    from app.workflows import build

    def default_responder(system, messages, tools):
        if tools and tools[0]["name"] == "split_user_message":
            user_msg = messages[-1]["content"].split("СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n")[-1]
            return {"tool_use": {"name": "split_user_message", "input": {
                "answer_text": user_msg, "comment_text": None, "rationale": "test default",
            }}}
        if tools and tools[0]["name"] == "update_state":
            return {"tool_use": {"name": "update_state", "input": {}}}
        return {"text": "Спасибо. Что дальше?"}

    mock = MockLLM(responder=default_responder)
    monkeypatch.setattr(agent, "_llm", mock)
    monkeypatch.setattr(agent, "_workflow", build("v2", llm=mock))
    monkeypatch.setattr(agent, "_store", FirestoreStore(collection=TEST_COLLECTION))
    monkeypatch.setattr(agent, "_warm_cache", {})
    yield agent, mock


def test_create_session_persists_with_chat_id(patched_agent, created_ids):
    agent, _mock = patched_agent

    session = agent.create_session(telegram_chat_id=42)
    created_ids.append(session.id)

    loaded = agent._store.load(session.id)
    snap = agent._store.client.collection(TEST_COLLECTION).document(session.id).get()
    assert loaded is not None
    assert snap.to_dict()["telegram_chat_id"] == 42
    assert loaded.transcript[0]["role"] == "assistant"


def _walk_through_screening(agent, session, telegram_chat_id):
    """Helper: 6 screening turns. Q1=name (free text), then 4 choices, plus email."""
    agent.reply(session, "Тест", telegram_chat_id=telegram_chat_id)
    agent.reply(session, "1", telegram_chat_id=telegram_chat_id)        # gender
    agent.reply(session, "1", telegram_chat_id=telegram_chat_id)        # age
    agent.reply(session, "test@example.com", telegram_chat_id=telegram_chat_id)
    agent.reply(session, "1", telegram_chat_id=telegram_chat_id)        # sector
    agent.reply(session, "1", telegram_chat_id=telegram_chat_id)        # time_eater


def test_reply_splits_and_feeds_only_answer_to_workflow(patched_agent, created_ids):
    agent, mock = patched_agent

    def custom_responder(system, messages, tools):
        if tools and tools[0]["name"] == "split_user_message":
            return {"tool_use": {"name": "split_user_message", "input": {
                "answer_text": "Нас трое.",
                "comment_text": "А зачем тебе это?",
                "rationale": "разделено вручную",
            }}}
        if tools and tools[0]["name"] == "update_state":
            return {"tool_use": {"name": "update_state", "input": {"point_a": {"team_size": 3}}}}
        return {"text": "Окей, что у вас за бизнес?"}

    mock.responder = custom_responder

    session = agent.create_session(telegram_chat_id=4242)
    created_ids.append(session.id)
    # Walk through the deterministic screening so we land in workflow phase.
    _walk_through_screening(agent, session, telegram_chat_id=4242)

    reply_text = agent.reply(session, "Нас трое. А зачем тебе это?", telegram_chat_id=4242)

    assert session.state.point_a.team_size == 3
    assert reply_text  # asker produced something

    snap = agent._store.client.collection(TEST_COLLECTION).document(session.id).get()
    classifications = snap.to_dict().get("classifications") or []
    # Filter to the workflow turn's classifications (they share the same kinds with
    # screening, but post-screening we expect the split-pipeline pair: "Нас трое."
    # as answer and "А зачем тебе это?" as comment).
    answer_texts = {c["text"] for c in classifications if c["type"] == "answer"}
    comment_texts = {c["text"] for c in classifications if c["type"] == "comment"}
    assert "Нас трое." in answer_texts
    assert "А зачем тебе это?" in comment_texts


def test_create_session_starts_with_screening_question(patched_agent, created_ids):
    agent, _ = patched_agent
    session = agent.create_session(telegram_chat_id=11_001)
    created_ids.append(session.id)

    greeting = session.transcript[0]["content"]
    # Новый Q1 — про имя.
    assert "Как могу к вам обращаться?" in greeting
    # NOT the old generative opener
    assert "и в конце соберу понятный план" not in greeting
    assert session.state.conversation.phase.value == "screening"
    assert session.state.conversation.screening_step == 0


def test_reply_during_screening_advances_step_and_persists_comment(patched_agent, created_ids):
    agent, _mock = patched_agent
    session = agent.create_session(telegram_chat_id=11_002)
    created_ids.append(session.id)

    # Q1 = name (свободный текст), без option-цифры; Q2 = gender — там можно
    # дать «1 + комментарий» как раньше.
    agent.reply(session, "Иван", telegram_chat_id=11_002)
    reply = agent.reply(session, "1 а зачем спрашиваете", telegram_chat_id=11_002)

    assert session.state.profile.name == "Иван"
    assert session.state.profile.gender == "female"
    assert session.state.conversation.screening_step == 2
    # Q3 теперь — возраст.
    assert "Сколько вам лет?" in reply

    snap = agent._store.client.collection(TEST_COLLECTION).document(session.id).get()
    classifications = snap.to_dict().get("classifications") or []
    comment_rows = [c for c in classifications if c["type"] == "comment"]
    assert any("зачем спрашиваете" in c["text"] for c in comment_rows)


def test_screening_completion_transitions_to_point_a_with_no_llm_call(patched_agent, created_ids):
    agent, mock = patched_agent
    session = agent.create_session(telegram_chat_id=11_003)
    created_ids.append(session.id)
    mock.calls.clear()

    agent.reply(session, "Анна", telegram_chat_id=11_003)               # Q1 name
    agent.reply(session, "1", telegram_chat_id=11_003)                  # Q2 gender = female
    agent.reply(session, "2", telegram_chat_id=11_003)                  # Q3 age = 25-35
    agent.reply(session, "anna@example.com", telegram_chat_id=11_003)   # Q4 email
    agent.reply(session, "1", telegram_chat_id=11_003)                  # Q5 sector = services
    final_reply = agent.reply(session, "1", telegram_chat_id=11_003)    # Q6 time_eater = client_comms

    # No LLM calls during deterministic screening (text+choice+email все детерминистичные).
    assert mock.calls == [], f"Unexpected LLM calls during screening: {len(mock.calls)}"

    assert session.state.profile.name == "Анна"
    assert session.state.profile.gender == "female"
    assert session.state.profile.age_range == "25-35"
    assert session.state.profile.email == "anna@example.com"
    assert session.state.profile.sector == "services"
    assert session.state.profile.time_eater == "client_comms"

    assert session.state.conversation.phase.value != "screening"
    assert "Спасибо" in final_reply


def test_post_screening_first_user_message_calls_llm(patched_agent, created_ids):
    agent, mock = patched_agent
    session = agent.create_session(telegram_chat_id=11_004)
    created_ids.append(session.id)
    _walk_through_screening(agent, session, telegram_chat_id=11_004)
    mock.calls.clear()

    agent.reply(session, "У меня магазин в Telegram, теряем заявки", telegram_chat_id=11_004)

    # LLM was called (split + extractor + asker)
    assert len(mock.calls) >= 1


def test_get_session_by_telegram_chat_finds_existing(patched_agent, created_ids):
    agent, _ = patched_agent

    session = agent.create_session(telegram_chat_id=7777)
    created_ids.append(session.id)
    # simulate cold-start: clear warm cache
    agent._warm_cache.clear()

    found = agent.get_session_by_telegram_chat(7777)

    assert found is not None
    assert found.id == session.id


def test_get_session_by_telegram_chat_returns_none_unknown(patched_agent):
    agent, _ = patched_agent
    assert agent.get_session_by_telegram_chat(987_654_321) is None
