"""Additional tests for app/telegram_bot.py — pure functions and edge cases."""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import httpx
import pytest


@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport, timeout=5.0)


class TestChunk:
    def test_short_text_single_chunk(self):
        from app.telegram_bot import _chunk

        result = _chunk("hello", 4000)
        assert result == ["hello"]

    def test_exact_boundary(self):
        from app.telegram_bot import _chunk

        text = "a" * 4000
        result = _chunk(text, 4000)
        assert result == [text]

    def test_splits_at_n(self):
        from app.telegram_bot import _chunk

        text = "a" * 8500
        result = _chunk(text, 4000)
        assert len(result) == 3
        assert "".join(result) == text

    def test_empty_string(self):
        from app.telegram_bot import _chunk

        result = _chunk("", 4000)
        assert result == [""]


class TestBotToken:
    def test_raises_when_not_set(self, monkeypatch):
        from app.telegram_bot import _bot_token

        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN is not set"):
            _bot_token()

    def test_returns_token(self):
        from app.telegram_bot import _bot_token

        assert _bot_token() == "TEST:TOKEN"


class TestEditMessageText:
    def test_success(self, monkeypatch):
        from app import telegram_bot

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        result = telegram_bot.edit_message_text(123, 456, "new text")
        assert result is True

    def test_failure_returns_false(self, monkeypatch):
        from app import telegram_bot

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"ok": False})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        result = telegram_bot.edit_message_text(123, 456, "text")
        assert result is False

    def test_long_text_returns_false(self, monkeypatch):
        from app import telegram_bot

        result = telegram_bot.edit_message_text(123, 456, "x" * 5000)
        assert result is False

    def test_with_reply_markup(self, monkeypatch):
        from app import telegram_bot

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}
        telegram_bot.edit_message_text(1, 2, "hi", reply_markup=markup)
        assert "reply_markup" in captured[0]


class TestDeleteMessage:
    def test_success(self, monkeypatch):
        from app import telegram_bot

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        telegram_bot.delete_message(123, 456)
        assert len(calls) == 1
        assert "deleteMessage" in calls[0]

    def test_failure_does_not_raise(self, monkeypatch):
        from app import telegram_bot

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"ok": False})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        # Should not raise
        telegram_bot.delete_message(123, 456)

    def test_exception_does_not_propagate(self, monkeypatch):
        from app import telegram_bot

        def bad_factory():
            raise ConnectionError("network down")

        monkeypatch.setattr(telegram_bot, "_client_factory", bad_factory)
        # Should not raise
        telegram_bot.delete_message(123, 456)


class TestSendChatAction:
    def test_sends_typing(self, monkeypatch):
        from app import telegram_bot

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        telegram_bot.send_chat_action(123)
        assert captured[0]["chat_id"] == 123
        assert captured[0]["action"] == "typing"

    def test_exception_does_not_propagate(self, monkeypatch):
        from app import telegram_bot

        def bad_factory():
            raise ConnectionError("network down")

        monkeypatch.setattr(telegram_bot, "_client_factory", bad_factory)
        telegram_bot.send_chat_action(123)


class TestAnswerCallbackQuery:
    def test_sends_answer(self, monkeypatch):
        from app import telegram_bot

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        telegram_bot.answer_callback_query("cb123", text="Done!")
        assert captured[0]["callback_query_id"] == "cb123"
        assert captured[0]["text"] == "Done!"

    def test_no_text(self, monkeypatch):
        from app import telegram_bot

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        telegram_bot.answer_callback_query("cb456")
        assert "text" not in captured[0]


class TestSendMessageReplyMarkup:
    def test_reply_markup_on_last_chunk(self, monkeypatch):
        from app import telegram_bot

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        markup = {"inline_keyboard": [[{"text": "Yes", "callback_data": "yes"}]]}
        telegram_bot.send_message(1, "a" * 5000, reply_markup=markup)
        # First chunk should not have markup
        assert "reply_markup" not in captured[0]
        # Last chunk should
        assert "reply_markup" in captured[-1]

    def test_returns_message_id(self, monkeypatch):
        from app import telegram_bot

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        mid = telegram_bot.send_message(1, "hi")
        assert mid == 42

    def test_handles_api_failure(self, monkeypatch):
        from app import telegram_bot

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"ok": False})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        mid = telegram_bot.send_message(1, "hi")
        assert mid is None


class TestHandleUpdateEdgeCases:
    def test_empty_update_is_noop(self, monkeypatch):
        from app import telegram_bot

        # Should not raise
        telegram_bot.handle_update({})

    def test_no_chat_id_is_noop(self, monkeypatch):
        from app import telegram_bot

        telegram_bot.handle_update({"message": {"text": "hello"}})

    def test_unsupported_content_type(self, monkeypatch):
        from app import telegram_bot

        sent: list[tuple] = []

        def _stub_send(chat_id, text, **kw):
            sent.append((chat_id, text))
            return 1

        monkeypatch.setattr(telegram_bot, "send_message", _stub_send)

        telegram_bot.handle_update({
            "message": {"chat": {"id": 99}, "photo": [{"file_id": "abc"}]},
        })
        # Should tell user about supported formats
        assert len(sent) == 1
        assert "текст" in sent[0][1].lower() or "голос" in sent[0][1].lower()


class TestSetWebhook:
    def test_sets_webhook(self, monkeypatch):
        from app import telegram_bot

        captured: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": True})

        monkeypatch.setattr(telegram_bot, "_client_factory", lambda: _make_client(handler))
        result = telegram_bot.set_webhook("https://example.com/webhook", secret_token="sec123")
        assert captured[0]["url"] == "https://example.com/webhook"
        assert captured[0]["secret_token"] == "sec123"
        assert result["ok"] is True


class TestSplitParagraphs:
    def test_short_text_no_split(self):
        from app.telegram_bot import _split_paragraphs

        result = _split_paragraphs("short text", 4096)
        assert result == ["short text"]

    def test_splits_on_paragraph_boundary(self):
        from app.telegram_bot import _split_paragraphs

        text = "A" * 50 + "\n\n" + "B" * 50
        result = _split_paragraphs(text, 60)
        assert len(result) == 2
        assert result[0] == "A" * 50
        assert result[1] == "B" * 50

    def test_hard_splits_oversized_paragraph(self):
        from app.telegram_bot import _split_paragraphs

        text = "X" * 100
        result = _split_paragraphs(text, 40)
        assert len(result) == 3
        assert "".join(result) == text

    def test_multiple_paragraphs_grouped(self):
        from app.telegram_bot import _split_paragraphs

        # Two short paragraphs that fit together
        text = "Hello\n\nWorld"
        result = _split_paragraphs(text, 100)
        assert result == ["Hello\n\nWorld"]


class TestExtractUserText:
    def test_text_message(self):
        from app.telegram_bot import _extract_user_text

        text, from_voice = _extract_user_text({"text": "hello"})
        assert text == "hello"
        assert from_voice is False

    def test_no_content_returns_none(self):
        from app.telegram_bot import _extract_user_text

        text, from_voice = _extract_user_text({"photo": [{"file_id": "x"}]})
        assert text is None
        assert from_voice is False

    def test_voice_message_transcribes(self, monkeypatch):
        from app import telegram_bot
        from app.telegram_bot import _extract_user_text

        monkeypatch.setattr(telegram_bot, "download_voice", lambda fid: b"audio")
        monkeypatch.setattr(telegram_bot, "transcribe", lambda blob: "Transcribed text")

        text, from_voice = _extract_user_text({"voice": {"file_id": "F123"}})
        assert text == "Transcribed text"
        assert from_voice is True

    def test_voice_transcription_failure(self, monkeypatch):
        from app import telegram_bot
        from app.telegram_bot import _extract_user_text

        def fail_download(fid):
            raise RuntimeError("network")

        monkeypatch.setattr(telegram_bot, "download_voice", fail_download)

        text, from_voice = _extract_user_text({"voice": {"file_id": "F123"}})
        assert "не удалось" in text
        assert from_voice is True


class TestAppendClarityScore:
    def test_no_scores_returns_original(self):
        from app.telegram_bot import _append_clarity_score

        session = MagicMock()
        session.state.solution.clarity_score = None
        result = _append_clarity_score(session, "Hello")
        assert result == "Hello"

    def test_all_zeros_returns_original(self):
        from app.telegram_bot import _append_clarity_score

        session = MagicMock()
        session.state.solution.clarity_score = {"point_a": 0, "point_b": 0, "resources": 0}
        result = _append_clarity_score(session, "Hello")
        assert result == "Hello"

    def test_appends_progress_indicator(self):
        from app.telegram_bot import _append_clarity_score

        session = MagicMock()
        session.state.solution.clarity_score = {"point_a": 80, "point_b": 50, "resources": 20}
        result = _append_clarity_score(session, "Hello")
        assert "80%" in result
        assert "50%" in result
        assert "20%" in result
        assert "Прогресс" in result


class TestDetermineReplyKeyboard:
    def test_returns_none_for_normal_phase(self):
        from app.telegram_bot import _determine_reply_keyboard
        from app.state import Phase

        session = MagicMock()
        session.state.conversation.phase = Phase.POINT_A
        session.transcript = []
        result = _determine_reply_keyboard(session)
        assert result is None

    def test_returns_keyboard_for_spec_review(self):
        from app.telegram_bot import _determine_reply_keyboard, SPEC_REVIEW_CONFIRM_KEYBOARD
        from app.state import Phase

        session = MagicMock()
        session.state.conversation.phase = Phase.SPEC_REVIEW
        session.transcript = [{"role": "assistant", "content": "spec", "meta": {}}]
        result = _determine_reply_keyboard(session)
        assert result == SPEC_REVIEW_CONFIRM_KEYBOARD

    def test_returns_none_on_exception(self):
        from app.telegram_bot import _determine_reply_keyboard

        session = MagicMock()
        type(session.state.conversation).phase = PropertyMock(side_effect=Exception("broken"))
        result = _determine_reply_keyboard(session)
        assert result is None


class TestAnalysisOptionsKeyboard:
    def test_builds_keyboard(self):
        from app.telegram_bot import _analysis_options_keyboard

        options = [
            {"shape": "CRM систему", "recommended": True},
            {"shape": "Telegram бот", "recommended": False},
        ]
        result = _analysis_options_keyboard(options)
        assert "inline_keyboard" in result
        buttons = result["inline_keyboard"]
        assert len(buttons) == 2
        assert "⭐" in buttons[0][0]["text"]
        assert buttons[0][0]["callback_data"] == "analysis_pick:1"
        assert buttons[1][0]["callback_data"] == "analysis_pick:2"

    def test_truncates_long_labels(self):
        from app.telegram_bot import _analysis_options_keyboard

        options = [{"shape": "A" * 100, "recommended": False}]
        result = _analysis_options_keyboard(options)
        label = result["inline_keyboard"][0][0]["text"]
        assert len(label) <= 60

    def test_max_three_options(self):
        from app.telegram_bot import _analysis_options_keyboard

        options = [{"shape": f"opt{i}", "recommended": False} for i in range(5)]
        result = _analysis_options_keyboard(options)
        assert len(result["inline_keyboard"]) == 3


class TestHandleCallbackQuery:
    def test_confirm_spec_callback(self, monkeypatch):
        from app import agent, telegram_bot

        sent: list[tuple] = []

        def stub_send(chat_id, text, **kw):
            sent.append((chat_id, text))
            return 1

        monkeypatch.setattr(telegram_bot, "send_message", stub_send)
        monkeypatch.setattr(telegram_bot, "send_chat_action", lambda *a, **k: None)
        monkeypatch.setattr(telegram_bot, "edit_message_text", lambda *a, **kw: True)
        monkeypatch.setattr(telegram_bot, "answer_callback_query", lambda *a, **k: None)

        stub_session = MagicMock()
        stub_session.id = "stub"
        stub_session.state.conversation.phase = "screening"
        stub_session.transcript = [{"role": "assistant", "content": "spec"}]

        monkeypatch.setattr(agent, "get_session_by_telegram_chat", lambda cid: stub_session)
        monkeypatch.setattr(agent, "reply", lambda s, m, **kw: "confirmed")

        telegram_bot.handle_update({
            "callback_query": {
                "id": "cb1",
                "data": "confirm_spec",
                "message": {"chat": {"id": 10}},
                "from": {"username": "testuser"},
            }
        })

    def test_analysis_pick_invalid_index(self, monkeypatch):
        from app import telegram_bot

        monkeypatch.setattr(telegram_bot, "answer_callback_query", lambda *a, **k: None)

        # Should not raise
        telegram_bot.handle_update({
            "callback_query": {
                "id": "cb2",
                "data": "analysis_pick:invalid",
                "message": {"chat": {"id": 10}},
                "from": {},
            }
        })

    def test_analysis_pick_out_of_range(self, monkeypatch):
        from app import telegram_bot

        monkeypatch.setattr(telegram_bot, "answer_callback_query", lambda *a, **k: None)

        telegram_bot.handle_update({
            "callback_query": {
                "id": "cb3",
                "data": "analysis_pick:5",
                "message": {"chat": {"id": 10}},
                "from": {},
            }
        })

    def test_unknown_callback_data_noop(self, monkeypatch):
        from app import telegram_bot

        monkeypatch.setattr(telegram_bot, "answer_callback_query", lambda *a, **k: None)

        # Should not raise
        telegram_bot.handle_update({
            "callback_query": {
                "id": "cb4",
                "data": "unknown_action",
                "message": {"chat": {"id": 10}},
                "from": {},
            }
        })
