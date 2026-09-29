"""Tests for /api/intake — web onboarding form endpoint.

Avoid Firestore by injecting a fake store into the agent module. Avoid
Turnstile by ensuring TURNSTILE_SECRET_KEY is unset in env (verify_turnstile
returns True in that case).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class FakeStore:
    sessions: dict = field(default_factory=dict)
    classifications: list[dict] = field(default_factory=list)

    def save(self, session, *, telegram_chat_id=None):
        self.sessions[session.id] = session

    def load(self, sid):
        return self.sessions.get(sid)

    def find_by_telegram_chat(self, chat_id):
        return None

    def append_classification(self, session_id, *, turn, kind, text, rationale):
        self.classifications.append({
            "session_id": session_id, "turn": turn, "kind": kind,
            "text": text, "rationale": rationale,
        })


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.rate_limit import rate_limiter
    rate_limiter._buckets.clear()
    yield
    rate_limiter._buckets.clear()


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with FakeStore injected and Turnstile disabled."""
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-for-is_configured")

    from fastapi.testclient import TestClient

    from app import agent
    from app.llm import MockLLM
    from app.workflows import build

    def responder(system, messages, tools):
        if tools and tools[0]["name"] == "split_user_message":
            user_msg = messages[-1]["content"].split("СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n")[-1]
            return {"tool_use": {"name": "split_user_message", "input": {
                "answer_text": user_msg, "comment_text": None, "rationale": "test",
            }}}
        if tools and tools[0]["name"] == "update_state":
            return {"tool_use": {"name": "update_state", "input": {}}}
        return {"text": "ok"}

    mock = MockLLM(responder=responder)
    store = FakeStore()
    monkeypatch.setattr(agent, "_llm", mock)
    monkeypatch.setattr(agent, "_workflow", build("v2", llm=mock))
    monkeypatch.setattr(agent, "_store", store)
    monkeypatch.setattr(agent, "_warm_cache", {})

    from app.main import app
    return TestClient(app), store


def _valid_payload():
    return {
        "name": "Анна",
        "email": "anna@example.com",
        "age_range": "25-35",
        "gender": "female",
        "sector": "services",
        "time_eater": "client_comms",
    }


def test_intake_happy_path_creates_session_with_prefilled_profile(client):
    tc, store = client
    res = tc.post("/api/intake", json=_valid_payload())
    assert res.status_code == 200, res.text
    body = res.json()
    sid = body["session_id"]
    assert body["chat_url"] == f"/chat?sid={sid}"

    session = store.sessions[sid]
    p = session.state.profile
    assert p.name == "Анна"
    assert p.email == "anna@example.com"
    assert p.age_range == "25-35"
    assert p.gender == "female"
    assert p.sector == "services"
    assert p.time_eater == "client_comms"
    # screening пропущен, фаза сразу POINT_A
    assert session.state.conversation.phase.value == "point_a"
    assert session.state.conversation.screening_step == 6
    # Welcome-сообщение в transcript обращается по имени
    assert session.transcript[0]["role"] == "assistant"
    assert "Анна" in session.transcript[0]["content"]


def test_intake_invalid_email_returns_400(client):
    tc, _ = client
    payload = _valid_payload() | {"email": "not-an-email"}
    res = tc.post("/api/intake", json=payload)
    # либо 400 от нашего regex, либо 422 от pydantic — проверим что 4xx
    assert 400 <= res.status_code < 500


def test_intake_missing_required_field_422(client):
    tc, _ = client
    payload = _valid_payload()
    del payload["age_range"]
    res = tc.post("/api/intake", json=payload)
    assert res.status_code == 422


def test_intake_invalid_enum_value_422(client):
    tc, _ = client
    payload = _valid_payload() | {"sector": "wrong-value"}
    res = tc.post("/api/intake", json=payload)
    assert res.status_code == 422


def test_intake_with_turnstile_token_when_disabled_succeeds(client):
    """Когда TURNSTILE_SECRET_KEY не задан — verify пропускается, любой токен ок."""
    tc, _ = client
    payload = _valid_payload() | {"turnstile_token": "anything"}
    res = tc.post("/api/intake", json=payload)
    assert res.status_code == 200


def test_intake_blocked_by_turnstile_when_enabled(client, monkeypatch):
    """Когда TURNSTILE_SECRET_KEY задан, но verify возвращает False — 400."""
    tc, _ = client
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    from app import captcha
    monkeypatch.setattr(captcha, "verify_turnstile", lambda token, **kw: False)
    res = tc.post("/api/intake", json=_valid_payload())
    assert res.status_code == 400
    assert "проверку" in res.json()["detail"]


def test_config_endpoint_returns_site_key():
    """GET /api/config возвращает site key (или пустую строку если не задан)."""
    from fastapi.testclient import TestClient
    from app.main import app
    tc = TestClient(app)
    res = tc.get("/api/config")
    assert res.status_code == 200
    assert "turnstile_site_key" in res.json()


def test_intake_triggers_sheets_sync(client, monkeypatch):
    """Web intake should schedule a Sheets sync immediately so the new session
    appears in the spreadsheet even before the owner sends the first chat turn.

    Regression guard: prior behaviour only triggered sync from the Telegram
    branch of create_session, leaving web-intake-only sessions invisible in
    Sheets until a chat message arrived.
    """
    tc, _ = client
    calls: list[None] = []
    from app import sheets_sync_runtime
    monkeypatch.setattr(
        sheets_sync_runtime, "schedule_sync",
        lambda *a, **kw: calls.append(None) or True,
    )
    res = tc.post("/api/intake", json=_valid_payload())
    assert res.status_code == 200
    assert len(calls) == 1


def test_intake_rate_limit_returns_429_after_5_requests(client):
    """6-й запрос за минуту с одного IP → 429."""
    tc, _ = client
    payload = _valid_payload()
    # 5 успешных запросов
    for _ in range(5):
        res = tc.post("/api/intake", json=payload)
        assert res.status_code == 200, res.text
    # 6-й — лимит превышен
    res = tc.post("/api/intake", json=payload)
    assert res.status_code == 429
    assert "запрос" in res.json()["detail"]


def test_intake_greeting_acks_time_eater_ru(client):
    """RU greeting references the pre-selected time_eater and asks a deeper question."""
    tc, store = client
    res = tc.post("/api/intake", json=_valid_payload())
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]
    greeting = store.sessions[sid].transcript[0]["content"]
    assert "общение с клиентами" in greeting
    assert "Расскажите подробнее" in greeting
    assert "Спасибо! Теперь расскажите своими словами" not in greeting


def test_intake_greeting_acks_time_eater_en(client):
    """EN greeting references the pre-selected time_eater and asks a deeper question."""
    tc, store = client
    payload = _valid_payload() | {"language": "en"}
    res = tc.post("/api/intake", json=payload)
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]
    greeting = store.sessions[sid].transcript[0]["content"]
    assert "client communications" in greeting
    assert "Tell me more" in greeting
    assert "Thanks! Now tell me in your own words" not in greeting


def test_intake_greeting_fallback_when_time_eater_other(client):
    """When time_eater is 'other' we fall back to the generic TRANSITION_TO_DISCOVERY message."""
    tc, store = client
    payload = _valid_payload() | {"time_eater": "other"}
    res = tc.post("/api/intake", json=payload)
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]
    greeting = store.sessions[sid].transcript[0]["content"]
    assert "Спасибо! Теперь расскажите своими словами" in greeting
