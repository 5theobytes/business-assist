"""Telegram webhook handler.

No python-telegram-bot — we use plain httpx for sendMessage / getFile and
google-cloud-speech for transcription. handle_update is sync and meant to be
called from a FastAPI BackgroundTask.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from . import agent
from .state import Phase

log = logging.getLogger("telegram_bot")

TG_API = "https://api.telegram.org"

# Placeholder shown while the bot processes a user's message.
TYPING_PLACEHOLDER_TEXT = "Обрабатываю ваш ответ... ⏳"

# Inline keyboard payload for the «Да, всё верно» confirmation at SPEC_REVIEW.
# Mirrors the web button (static/app.js:113) — same canonical confirmation
# text gets sent back through agent.reply, so PM-LLM treats both surfaces
# identically.
SPEC_REVIEW_CONFIRM_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "✅ Да, вопросов нет, подтверждаю", "callback_data": "confirm_spec"},
    ]],
}
# Alias for v4 checkpoint confirmation — same keyboard, different context.
CHECKPOINT_CONFIRM_KEYBOARD = {
    "inline_keyboard": [[
        {"text": "✅ Да, вопросов нет, подтверждаю", "callback_data": "confirm_checkpoint"},
    ]],
}
CONFIRM_BUTTON_PAYLOAD = "да, вопросов нет, подтверждаю"


def _bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return token


# Indirected so unit tests can swap the underlying transport via monkeypatch.
def _client_factory() -> httpx.Client:
    return httpx.Client(timeout=30.0)


def send_message(
    chat_id: int,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> int | None:
    """Send a Telegram message. Returns message_id of the LAST chunk sent
    (TG splits long messages — only the final chunk gets the reply_markup
    so the keyboard sits under the visible bubble), or None on failure.
    """
    token = _bot_token()
    url = f"{TG_API}/bot{token}/sendMessage"
    chunks = _chunk(text, 4000)
    last_message_id: int | None = None
    with _client_factory() as client:
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and i == len(chunks) - 1:
                payload["reply_markup"] = json.dumps(reply_markup)
            r = client.post(url, json=payload)
            if r.status_code >= 400:
                log.warning("telegram sendMessage failed: %s %s", r.status_code, r.text)
                continue
            try:
                last_message_id = (r.json().get("result") or {}).get("message_id")
            except Exception:
                last_message_id = None
    return last_message_id


def edit_message_text(
    chat_id: int,
    message_id: int,
    text: str,
    *,
    reply_markup: dict | None = None,
) -> bool:
    """Edit an existing message's text. Returns True on success, False on failure.

    Used to replace the placeholder with the actual LLM response so users
    see a seamless transition instead of delete+new-message flicker.
    """
    token = _bot_token()
    # Telegram's editMessageText max is 4096; if longer, fallback to delete+send.
    if len(text) > 4096:
        return False
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        with _client_factory() as client:
            r = client.post(f"{TG_API}/bot{token}/editMessageText", json=payload)
            if r.status_code >= 400:
                log.info(
                    "telegram editMessageText failed (%s): %s", r.status_code, r.text,
                )
                return False
        return True
    except Exception:
        log.info("telegram editMessageText failed", exc_info=True)
        return False


def delete_message(chat_id: int, message_id: int) -> None:
    """Best-effort delete. Fallback when editMessageText can't be used."""
    token = _bot_token()
    try:
        with _client_factory() as client:
            r = client.post(
                f"{TG_API}/bot{token}/deleteMessage",
                json={"chat_id": chat_id, "message_id": message_id},
            )
            if r.status_code >= 400:
                log.info(
                    "telegram deleteMessage skipped (%s): %s", r.status_code, r.text,
                )
    except Exception:
        log.info("telegram deleteMessage failed", exc_info=True)


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    """Native TG «typing» indicator (≈5s). Pairs with the placeholder bubble."""
    token = _bot_token()
    try:
        with _client_factory() as client:
            client.post(
                f"{TG_API}/bot{token}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
            )
    except Exception:
        log.info("telegram sendChatAction failed", exc_info=True)


def answer_callback_query(callback_query_id: str, text: str | None = None) -> None:
    """Acknowledge an inline-keyboard tap so the spinner clears on the user's
    side. Telegram requires this within 15s or the button stays in 'loading'."""
    token = _bot_token()
    try:
        with _client_factory() as client:
            payload: dict[str, Any] = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            client.post(
                f"{TG_API}/bot{token}/answerCallbackQuery", json=payload,
            )
    except Exception:
        log.info("telegram answerCallbackQuery failed", exc_info=True)


def _chunk(s: str, n: int) -> list[str]:
    if len(s) <= n:
        return [s]
    return [s[i : i + n] for i in range(0, len(s), n)]


def download_voice(file_id: str) -> bytes:
    token = _bot_token()
    with _client_factory() as client:
        meta = client.get(f"{TG_API}/bot{token}/getFile", params={"file_id": file_id})
        meta.raise_for_status()
        file_path = meta.json()["result"]["file_path"]
        blob = client.get(f"{TG_API}/file/bot{token}/{file_path}")
        blob.raise_for_status()
        return blob.content


def transcribe(audio_bytes: bytes, language_code: str = "ru-RU") -> str:
    from google.cloud import speech

    client = speech.SpeechClient()
    audio = speech.RecognitionAudio(content=audio_bytes)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS,
        sample_rate_hertz=48000,
        language_code=language_code,
        enable_automatic_punctuation=True,
    )
    resp = client.recognize(config=config, audio=audio)
    parts = [r.alternatives[0].transcript for r in resp.results if r.alternatives]
    return " ".join(parts).strip()


def set_webhook(public_url: str, *, secret_token: str | None = None) -> dict:
    token = _bot_token()
    payload: dict[str, Any] = {"url": public_url}
    if secret_token:
        payload["secret_token"] = secret_token
    with _client_factory() as client:
        r = client.post(f"{TG_API}/bot{token}/setWebhook", json=payload)
        r.raise_for_status()
        return r.json()


def handle_update(update: dict[str, Any]) -> None:
    """Single entry-point. Called from FastAPI BackgroundTasks."""
    # Inline-keyboard callbacks (e.g. tap on «Да, всё верно» at SPEC_REVIEW).
    # Treated as if the user typed CONFIRM_BUTTON_PAYLOAD.
    callback = update.get("callback_query")
    if callback:
        _handle_callback_query(callback)
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return

    try:
        user_text, from_voice = _extract_user_text(msg)
        if user_text is None:
            send_message(chat_id, "Я понимаю текст и голосовые. Пришлите одно из них.")
            return

        # @username (без знака @) — для будущей связи. У некоторых пользователей
        # его нет (приватные настройки) — тогда останется только chat_id.
        from_field = msg.get("from") or {}
        tg_username = from_field.get("username") or None

        if user_text.strip().lower().startswith("/start"):
            session = agent.create_session(
                telegram_chat_id=chat_id, telegram_username=tg_username,
            )
            send_message(chat_id, session.transcript[0]["content"])
            return

        session = agent.get_session_by_telegram_chat(chat_id)
        if session is None:
            session = agent.create_session(
                telegram_chat_id=chat_id, telegram_username=tg_username,
            )
            send_message(chat_id, session.transcript[0]["content"])

        _send_reply_with_placeholder(
            chat_id, session, user_text,
            telegram_username=tg_username, from_voice=from_voice,
        )
    except Exception as exc:  # noqa: BLE001 — single safety net for the update handler
        log.exception("telegram update handler crashed")
        try:
            from anthropic import RateLimitError
            if isinstance(exc, RateLimitError):
                msg = (
                    "Сейчас слишком много запросов — это лимит платформы, "
                    "не ваша вина. Попробуйте, пожалуйста, через минуту."
                )
            else:
                msg = "Что-то сломалось на нашей стороне. Попробуйте ещё раз через минуту."
            send_message(chat_id, msg)
        except Exception:
            log.exception("failed to notify user about crash")


def _handle_callback_query(callback: dict[str, Any]) -> None:
    """Translate an inline-keyboard tap into a regular agent.reply turn.

    Handles: confirm_spec, confirm_checkpoint, analysis_pick:N.
    Any unknown callback_data is acknowledged silently.
    """
    cb_id = callback.get("id")
    data = (callback.get("data") or "").strip()
    msg = callback.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    from_field = callback.get("from") or {}
    tg_username = from_field.get("username") or None

    if cb_id:
        answer_callback_query(cb_id)
    if not chat_id:
        return

    if data in ("confirm_spec", "confirm_checkpoint"):
        session = agent.get_session_by_telegram_chat(chat_id)
        if session is None:
            send_message(
                chat_id,
                "Эта сессия уже завершена. Напишите /start, чтобы начать заново.",
            )
            return
        try:
            _send_reply_with_placeholder(
                chat_id, session, CONFIRM_BUTTON_PAYLOAD,
                telegram_username=tg_username, from_voice=False,
            )
        except Exception:
            log.exception("telegram confirm callback failed")
            send_message(
                chat_id,
                "Что-то сломалось на нашей стороне. Попробуйте ещё раз через минуту.",
            )
        return

    # ANALYSIS option pick: data == "analysis_pick:1|2|3". Translate to text
    # message «выбираю вариант N», push through the regular reply path so PM-LLM
    # classifies it as `pick`.
    if data.startswith("analysis_pick:"):
        try:
            idx = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if idx < 1 or idx > 3:
            return
        session = agent.get_session_by_telegram_chat(chat_id)
        if session is None:
            send_message(
                chat_id,
                "Эта сессия уже завершена. Напишите /start, чтобы начать заново.",
            )
            return
        payload = f"выбираю вариант {idx}"
        try:
            _send_reply_with_placeholder(
                chat_id, session, payload,
                telegram_username=tg_username, from_voice=False,
            )
        except Exception:
            log.exception("telegram analysis_pick callback failed")
            send_message(
                chat_id,
                "Что-то сломалось на нашей стороне. Попробуйте ещё раз через минуту.",
            )


def _send_reply_with_placeholder(
    chat_id: int,
    session,
    user_text: str,
    *,
    telegram_username: str | None,
    from_voice: bool,
) -> None:
    """Wrap agent.reply with a placeholder so the user sees immediate feedback.

    Flow:
      1. Send typing action + placeholder message
      2. Process through agent.reply() (7-30s)
      3. Edit the placeholder with the real response (editMessageText)
      4. If edit fails (too long, stale, etc.) — delete placeholder + send new

    Placeholder is suppressed during SCREENING — the screening loop is fully
    deterministic (no LLM round-trip), the next question lands instantly.
    """
    in_screening = False
    try:
        in_screening = session.state.conversation.phase == Phase.SCREENING
    except Exception:
        pass

    placeholder_id: int | None = None
    if not in_screening:
        send_chat_action(chat_id, "typing")
        placeholder_id = send_message(chat_id, TYPING_PLACEHOLDER_TEXT)

    try:
        reply_text = agent.reply(
            session, user_text,
            telegram_chat_id=chat_id,
            telegram_username=telegram_username,
            from_voice=from_voice,
        )
    except Exception:
        # Edit placeholder with friendly error, then re-raise for outer handler.
        if placeholder_id is not None:
            edited = edit_message_text(
                chat_id, placeholder_id,
                "Что-то сломалось на нашей стороне. Попробуйте ещё раз через минуту.",
            )
            if not edited:
                delete_message(chat_id, placeholder_id)
        raise

    # Append clarity score progress if available.
    reply_text = _append_clarity_score(session, reply_text)

    # Determine keyboard based on post-reply phase.
    reply_markup = _determine_reply_keyboard(session)

    # Check if session is done → deliver three documents.
    is_done = False
    try:
        is_done = session.state.conversation.phase == Phase.DONE
    except Exception:
        pass

    # Try to edit the placeholder with the reply.
    edited = False
    if placeholder_id is not None:
        edited = edit_message_text(
            chat_id, placeholder_id, reply_text,
            reply_markup=reply_markup,
        )
        if not edited:
            # Fallback: delete placeholder and send as new message.
            delete_message(chat_id, placeholder_id)
            send_message(chat_id, reply_text, reply_markup=reply_markup)
    else:
        send_message(chat_id, reply_text, reply_markup=reply_markup)

    # Deliver three documents when session is done.
    if is_done:
        _deliver_documents(chat_id, session)


def _determine_reply_keyboard(session) -> dict | None:
    """Choose the appropriate inline keyboard for the current session state."""
    try:
        phase = session.state.conversation.phase
        if phase == Phase.SPEC_REVIEW:
            return SPEC_REVIEW_CONFIRM_KEYBOARD
        if phase == Phase.ANALYSIS:
            options = list(session.state.solution.offered_options or [])
            last_meta = (session.transcript[-1].get("meta") or {}) if session.transcript else {}
            kind = (last_meta.get("kind") or "")
            if options and kind in ("analysis_options", "analysis_options_reemit"):
                return _analysis_options_keyboard(options)
        # Clarity checkpoint: show confirm button when the reply is a checkpoint.
        last_meta = (session.transcript[-1].get("meta") or {}) if session.transcript else {}
        kind = (last_meta.get("kind") or "")
        if kind == "clarity_checkpoint":
            return CHECKPOINT_CONFIRM_KEYBOARD
    except Exception:
        log.info("could not read session phase for keyboard", exc_info=True)
    return None


def _analysis_options_keyboard(options: list[dict]) -> dict:
    """Build inline_keyboard with one button per ANALYSIS option."""
    buttons = []
    for i, opt in enumerate(options[:3], start=1):
        shape = (opt.get("shape") or f"вариант {i}").strip()
        recommended = opt.get("recommended", False)
        star = "⭐ " if recommended else ""
        # Telegram caps button text at ~64 chars. Keep label short.
        label = f"{star}Вариант {i}: {shape}"
        if len(label) > 60:
            label = label[:57] + "…"
        buttons.append([{"text": label, "callback_data": f"analysis_pick:{i}"}])
    return {"inline_keyboard": buttons}


def _append_clarity_score(session, text: str) -> str:
    """Append clarity progress indicator if scores are available."""
    try:
        scores = session.state.solution.clarity_score
        if not scores:
            return text
        point_a = scores.get("point_a", 0)
        point_b = scores.get("point_b", 0)
        resources = scores.get("resources", 0)
        # Only show if there's any actual progress.
        if point_a == 0 and point_b == 0 and resources == 0:
            return text
        indicator = (
            f"\n\n📊 Прогресс: Точка А — {point_a}% | "
            f"Точка Б — {point_b}% | Ресурсы — {resources}%"
        )
        return text + indicator
    except Exception:
        return text


def _deliver_documents(chat_id: int, session) -> None:
    """Send the three output documents when the session is done.

    Each document gets a header. If a document exceeds 4096 chars (TG limit),
    it's split on paragraph boundaries (\n\n).
    """
    docs = [
        ("📋 **Ваша ясность** (Документ 1 из 3)", getattr(session, "clarity_doc", None)),
        ("📄 **Бриф для разработчика** (Документ 2 из 3)", getattr(session, "business_spec", None)),
        ("🔧 **Техническая спецификация** (Документ 3 из 3)", getattr(session, "dev_spec", None)),
    ]
    for header, content in docs:
        if not content:
            continue
        full_text = f"{header}\n\n{content}"
        chunks = _split_paragraphs(full_text, 4096)
        for chunk in chunks:
            send_message(chat_id, chunk)


def _split_paragraphs(text: str, max_len: int) -> list[str]:
    """Split text on paragraph boundaries (\n\n) respecting max_len.

    If a single paragraph exceeds max_len, fall back to hard splitting.
    """
    if len(text) <= max_len:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # If a single paragraph is too long, hard-split it.
            if len(para) > max_len:
                for i in range(0, len(para), max_len):
                    chunks.append(para[i:i + max_len])
                current = ""
            else:
                current = para
    if current:
        chunks.append(current)
    return chunks


def _extract_user_text(msg: dict[str, Any]) -> tuple[str | None, bool]:
    """Returns (text, from_voice). from_voice=True when text came from STT."""
    text = msg.get("text")
    if text:
        return text, False
    voice = msg.get("voice") or msg.get("audio")
    if voice and voice.get("file_id"):
        try:
            audio = download_voice(voice["file_id"])
            transcript = transcribe(audio)
            return (transcript or "(пустая транскрипция)"), True
        except Exception:
            log.exception("voice transcription failed")
            return "(не удалось распознать голосовое)", True
    return None, False
