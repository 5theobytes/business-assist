"""Tests for app/telegram_bot.py — webhook handler + helpers."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest


@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")


def _make_client(handler):
    """Build a real httpx.Client backed by MockTransport(handler)."""
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=5.0)


def test_send_message_posts_to_telegram(monkeypatch):
    from app import telegram_bot

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append({"url": str(request.url), "body": body})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))

    telegram_bot.send_message(123, "Привет")

    assert len(captured) == 1
    assert "/botTEST:TOKEN/sendMessage" in captured[0]["url"]
    assert captured[0]["body"] == {"chat_id": 123, "text": "Привет"}


def test_send_message_chunks_long_text(monkeypatch):
    from app import telegram_bot

    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))

    long_text = "x" * 9000
    telegram_bot.send_message(1, long_text)

    assert len(posted) == 3   # 4000 + 4000 + 1000
    assert "".join(posted) == long_text


def test_handle_update_text_creates_session_and_replies(monkeypatch):
    from app import agent, telegram_bot

    sent: list[tuple[int, str]] = []
    def _stub_send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return 1
    monkeypatch.setattr(telegram_bot, "send_message", _stub_send)
    monkeypatch.setattr(telegram_bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "delete_message", lambda *a, **k: None)

    class _StubSession:
        id = "stub"
        def __init__(self):
            self.transcript = [{"role": "assistant", "content": "Greeting!"}]

    monkeypatch.setattr(agent, "get_session_by_telegram_chat", lambda cid: None)
    monkeypatch.setattr(agent, "create_session", lambda **kw: _StubSession())
    monkeypatch.setattr(agent, "reply", lambda s, m, **kw: f"echo:{m}")

    telegram_bot.handle_update({
        "message": {"chat": {"id": 555}, "text": "Hello"},
    })

    assert (555, "Greeting!") in sent
    assert (555, "echo:Hello") in sent


def test_handle_update_start_always_creates_new_session(monkeypatch):
    from app import agent, telegram_bot

    sent: list[tuple[int, str]] = []
    def _stub_send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return 1
    monkeypatch.setattr(telegram_bot, "send_message", _stub_send)
    monkeypatch.setattr(telegram_bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "delete_message", lambda *a, **k: None)

    creates: list[int | None] = []

    class _StubSession:
        id = "stub"
        transcript = [{"role": "assistant", "content": "Привет, давайте начнём."}]

    def fake_create(**kw):
        creates.append(kw.get("telegram_chat_id"))
        return _StubSession()

    # Even if a session exists for this chat, /start ignores it.
    monkeypatch.setattr(agent, "get_session_by_telegram_chat", lambda cid: _StubSession())
    monkeypatch.setattr(agent, "create_session", fake_create)
    monkeypatch.setattr(agent, "reply", lambda *a, **k: pytest.fail("reply must not be called on /start"))

    telegram_bot.handle_update({
        "message": {"chat": {"id": 99}, "text": "/start"},
    })

    assert creates == [99]
    assert sent == [(99, "Привет, давайте начнём.")]


def test_handle_update_voice_transcribes_then_replies(monkeypatch):
    from app import agent, telegram_bot

    sent: list[tuple[int, str]] = []
    def _stub_send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return 1
    monkeypatch.setattr(telegram_bot, "send_message", _stub_send)
    monkeypatch.setattr(telegram_bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "delete_message", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "download_voice", lambda fid: b"fake-ogg-bytes")
    monkeypatch.setattr(telegram_bot, "transcribe", lambda blob: "Распознанный голос")

    class _StubSession:
        id = "stub"
        transcript = [{"role": "assistant", "content": "G"}]

    monkeypatch.setattr(agent, "get_session_by_telegram_chat", lambda cid: _StubSession())
    captured: dict = {}
    def fake_reply(s, m, **kw):
        captured["msg"] = m
        captured["chat_id"] = kw.get("telegram_chat_id")
        captured["from_voice"] = kw.get("from_voice", False)
        return "ok"
    monkeypatch.setattr(agent, "reply", fake_reply)

    telegram_bot.handle_update({
        "message": {"chat": {"id": 31}, "voice": {"file_id": "FILE123"}},
    })

    assert captured["msg"] == "Распознанный голос"
    assert captured["chat_id"] == 31
    assert captured["from_voice"] is True   # voice path должен установить флаг
    assert (31, "ok") in sent


def test_handle_update_sends_fallback_when_reply_crashes(monkeypatch):
    from app import agent, telegram_bot

    sent: list[tuple[int, str]] = []
    def _stub_send(chat_id, text, **kw):
        sent.append((chat_id, text))
        return 1
    monkeypatch.setattr(telegram_bot, "send_message", _stub_send)
    monkeypatch.setattr(telegram_bot, "send_chat_action", lambda *a, **k: None)
    monkeypatch.setattr(telegram_bot, "delete_message", lambda *a, **k: None)

    class _StubSession:
        id = "stub"
        transcript = [{"role": "assistant", "content": "g"}]

    monkeypatch.setattr(agent, "get_session_by_telegram_chat", lambda cid: _StubSession())
    def boom(*a, **k):
        raise RuntimeError("downstream blew up")
    monkeypatch.setattr(agent, "reply", boom)

    telegram_bot.handle_update({
        "message": {"chat": {"id": 7}, "text": "что"},
    })

    # last sent message must be the apology
    assert sent[-1][0] == 7
    assert "сломалось" in sent[-1][1].lower()
