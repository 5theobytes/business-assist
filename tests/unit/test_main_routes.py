"""Route-level tests via FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.state import Phase, WorkflowState
from app.workflows.base import WorkflowSession


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    # Prevent agent module from trying to instantiate AnthropicLLM
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from app.main import app
    return TestClient(app)


def test_webhook_rejects_bad_secret(client):
    r = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 1}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert r.status_code == 401


def test_webhook_accepts_correct_secret(client, monkeypatch):
    from app import telegram_bot

    captured: list[dict] = []
    monkeypatch.setattr(telegram_bot, "handle_update", lambda update: captured.append(update))

    r = client.post(
        "/telegram/webhook",
        json={"message": {"chat": {"id": 1}, "text": "hi"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "supersecret"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # BackgroundTasks are executed by TestClient before the response returns
    assert len(captured) == 1


def test_admin_set_webhook_rejects_without_token(client):
    r = client.post("/telegram/admin/set-webhook?public_url=https://x")
    assert r.status_code == 401


def test_admin_set_webhook_calls_through_with_correct_token(client, monkeypatch):
    from app import telegram_bot

    called: dict = {}
    def fake_set(public_url, *, secret_token=None):
        called["public_url"] = public_url
        called["secret_token"] = secret_token
        return {"ok": True, "result": True}
    monkeypatch.setattr(telegram_bot, "set_webhook", fake_set)

    r = client.post(
        "/telegram/admin/set-webhook?public_url=https://example.onrender.com/telegram/webhook",
        headers={"X-Admin-Token": "admintoken"},
    )
    assert r.status_code == 200
    assert called["public_url"] == "https://example.onrender.com/telegram/webhook"
    assert called["secret_token"] == "supersecret"


def test_health_returns_basic_info(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["workflow"] == "v2"
    assert body["configured"] is True


def test_root_opens_the_tool_intake_flow(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/intake"


def test_lifespan_auto_registers_webhook_with_render_external_url(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")

    from app import telegram_bot
    from app.main import app

    called: dict = {}
    def fake_set(public_url, *, secret_token=None):
        called["public_url"] = public_url
        called["secret_token"] = secret_token
        return {"ok": True, "result": True}
    monkeypatch.setattr(telegram_bot, "set_webhook", fake_set)

    with TestClient(app):
        pass

    assert called["public_url"] == "https://example.onrender.com/telegram/webhook"
    assert called["secret_token"] == "supersecret"


def test_lifespan_skips_webhook_without_bot_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")

    from app import telegram_bot
    from app.main import app

    called: list = []
    monkeypatch.setattr(
        telegram_bot, "set_webhook",
        lambda *a, **kw: called.append((a, kw)) or {"ok": True},
    )

    with TestClient(app):
        pass

    assert called == []


def test_lifespan_skips_webhook_without_public_url(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    monkeypatch.delenv("PUBLIC_URL", raising=False)

    from app import telegram_bot
    from app.main import app

    called: list = []
    monkeypatch.setattr(
        telegram_bot, "set_webhook",
        lambda *a, **kw: called.append((a, kw)) or {"ok": True},
    )

    with TestClient(app):
        pass

    assert called == []


def test_lifespan_swallows_webhook_errors(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://example.onrender.com")

    from app import telegram_bot
    from app.main import app

    def boom(*a, **kw):
        raise RuntimeError("telegram api timeout")
    monkeypatch.setattr(telegram_bot, "set_webhook", boom)

    # Must not raise — startup is allowed to fail webhook registration.
    with TestClient(app):
        pass


def _make_session_with_suggested_options() -> WorkflowSession:
    """Build a session whose last assistant message has suggested_options."""
    session = WorkflowSession(id="chat-test-sid", workflow_name="v4")
    session.state = WorkflowState()
    session.state.conversation.phase = Phase.POINT_A
    session.state.conversation.language = "ru"
    session.transcript = [
        {"role": "assistant", "content": "Привет!", "meta": {"phase": "point_a", "suggested_options": ["Расскажу про бизнес", "Есть проблема"]}},
        {"role": "user", "content": "Расскажу про бизнес"},
        {"role": "assistant", "content": "Сколько сотрудников?", "meta": {"phase": "point_a", "suggested_options": ["1-3", "4-10", "10+"]}},
    ]
    return session


def test_chat_response_includes_suggested_options(client, monkeypatch):
    """ChatResponse from /api/chat includes suggested_options from meta."""
    from app import agent

    session = _make_session_with_suggested_options()
    monkeypatch.setattr(agent, "is_configured", lambda: True)
    monkeypatch.setattr(agent, "get_session", lambda sid: session)
    # Simulate a reply that appends an assistant message with suggested_options
    def fake_reply(sess, message, **kwargs):
        sess.transcript.append({"role": "user", "content": message})
        sess.transcript.append({
            "role": "assistant",
            "content": "Какой у вас бюджет?",
            "meta": {"phase": "resources", "suggested_options": ["До 10 000", "10 000-50 000"]},
        })
        return "Какой у вас бюджет?"
    monkeypatch.setattr(agent, "reply", fake_reply)

    r = client.post("/api/chat", json={"session_id": "chat-test-sid", "message": "наши расходы"})
    assert r.status_code == 200
    body = r.json()
    assert body["suggested_options"] == ["До 10 000", "10 000-50 000"]
    assert body["phase_marker"] == "resources"
    assert body["allow_custom_input"] is True


def test_chat_response_suggested_options_empty_when_no_meta(client, monkeypatch):
    """ChatResponse has empty suggested_options when last assistant meta lacks them."""
    from app import agent

    session = WorkflowSession(id="no-meta-sid", workflow_name="v4")
    session.state = WorkflowState()
    session.state.conversation.phase = Phase.POINT_A
    session.state.conversation.language = "ru"
    session.transcript = [
        {"role": "assistant", "content": "Привет!"},
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "Расскажите о бизнесе."},
    ]
    monkeypatch.setattr(agent, "is_configured", lambda: True)
    monkeypatch.setattr(agent, "get_session", lambda sid: session)

    def fake_reply(sess, message, **kwargs):
        sess.transcript.append({"role": "user", "content": message})
        sess.transcript.append({"role": "assistant", "content": "Спасибо."})
        return "Спасибо."
    monkeypatch.setattr(agent, "reply", fake_reply)

    r = client.post("/api/chat", json={"session_id": "no-meta-sid", "message": "ok"})
    assert r.status_code == 200
    body = r.json()
    assert body["suggested_options"] == []
    assert body["phase_marker"] is None


def test_session_state_includes_last_suggested_options(client, monkeypatch):
    """GET /api/session/{sid} includes last_suggested_options."""
    from app import agent

    session = _make_session_with_suggested_options()
    monkeypatch.setattr(agent, "get_session", lambda sid: session)

    r = client.get(f"/api/session/{session.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["last_suggested_options"] == ["1-3", "4-10", "10+"]
    assert body["phase_marker"] == "point_a"


def test_session_state_last_suggested_options_empty_when_none(client, monkeypatch):
    """GET /api/session/{sid} has empty last_suggested_options when no assistant meta."""
    from app import agent

    session = WorkflowSession(id="empty-meta-sid", workflow_name="v4")
    session.state = WorkflowState()
    session.state.conversation.phase = Phase.POINT_A
    session.state.conversation.language = "ru"
    session.transcript = [
        {"role": "assistant", "content": "Привет!"},
    ]
    monkeypatch.setattr(agent, "get_session", lambda sid: session)

    r = client.get(f"/api/session/{session.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["last_suggested_options"] == []
    assert body["phase_marker"] == "point_a"
