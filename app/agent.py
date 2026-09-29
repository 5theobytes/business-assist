"""Session orchestration.

Pipeline на один user-turn:
  1. Если phase == SCREENING — детерминированный survey loop (см. app/screening.py),
     никаких LLM-вызовов; comment-часть всё равно сохраняется как kind="comment"
  2. Иначе:
     a. classifier.split → (answer_text, comment_text)
     b. SessionStore.append_classification × {answer, comment} (что не None)
     c. workflow.respond(session, answer_text)
  3. SessionStore.save(session)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from . import bootstrap  # noqa: F401  side-effect: GCP creds before google import
from . import classifier, comments, screening, sheets_sync_runtime
from .llm import LLM, get_default_llm
from .session_store import SessionStore, get_default_store
from .state import Phase, WorkflowState
from .workflows import Workflow, WorkflowSession, build


def _trigger_sheets_sync() -> None:
    """Onlайн-sync в Sheets, debounced. Не падаем, если что-то не так."""
    try:
        sheets_sync_runtime.schedule_sync()
    except Exception:
        # Sync — best-effort: ошибки не должны валить чат.
        import logging
        logging.getLogger("agent").exception("sheets_sync_runtime.schedule_sync failed")

DEFAULT_WORKFLOW = os.environ.get("WORKFLOW", "v2")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

_workflow: Workflow | None = None
_llm: LLM | None = None
_store: SessionStore | None = None
_warm_cache: dict[str, WorkflowSession] = {}

_TIME_EATER_LABELS: dict[str, dict[str, str]] = {
    "client_comms": {"ru": "общение с клиентами", "en": "client communications"},
    "sales": {"ru": "продажи", "en": "sales"},
    "production": {"ru": "производство/оказание услуг", "en": "production/service delivery"},
    "admin": {"ru": "административные задачи", "en": "admin tasks"},
    "marketing": {"ru": "маркетинг и продвижение", "en": "marketing"},
    "other": {"ru": "другое", "en": "other"},
}


def _get_workflow() -> Workflow:
    global _workflow, _llm
    if _workflow is None:
        _llm = get_default_llm()
        _workflow = build(DEFAULT_WORKFLOW, llm=_llm)
    return _workflow


def _get_store() -> SessionStore:
    global _store
    if _store is None:
        _store = get_default_store()
    return _store


def create_session(
    *,
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
    prefilled_profile: dict | None = None,
    language: str = "ru",
) -> WorkflowSession:
    """Create a new session.

    Two modes:

    - **Telegram (default):** screening starts from step 0; bot эмитит первый
      вопрос («Как могу к вам обращаться?»). Шаги идут последовательно через
      `_handle_screening_turn`.
    - **Web (`prefilled_profile` задан):** все 6 полей профиля заполняются разом
      из intake-формы. Screening считается завершённым (`screening_step =
      len(SCREENING_QUESTIONS)`), фаза сразу POINT_A. В transcript кладётся
      короткое welcome-сообщение, обращающееся по имени.
    """
    wf = _get_workflow()
    session = wf.new_session()
    session.state.conversation.started_at = datetime.now(timezone.utc)
    session.state.conversation.language = language
    if telegram_username:
        session.state.profile.telegram_username = telegram_username

    if prefilled_profile:
        for k, v in prefilled_profile.items():
            if hasattr(session.state.profile, k):
                setattr(session.state.profile, k, v)
        session.state.conversation.phase = Phase.POINT_A
        session.state.conversation.screening_step = len(screening.SCREENING_QUESTIONS[language])
        name = (prefilled_profile.get("name") or "").strip()
        time_eater = (prefilled_profile.get("time_eater") or "").strip()
        time_eater_label = _TIME_EATER_LABELS.get(time_eater, {}).get(language)

        if time_eater and time_eater != "other" and time_eater_label:
            if language == "en":
                greet = (
                    f"Hi, {name}! " if name else "Hi! "
                ) + (
                    f"You mentioned that {time_eater_label} takes up most of your time. "
                    "Tell me more — can you give a specific example from your last week?"
                )
                suggested_opts = [
                    "Here's a recent example",
                    "It happens every day",
                    "I'm not sure where to start",
                ]
            else:
                greet = (
                    f"Здравствуйте, {name}! " if name else "Здравствуйте! "
                ) + (
                    f"Вы отметили, что больше всего времени забирает {time_eater_label}. "
                    "Расскажите подробнее — можете привести конкретный пример из последней недели?"
                )
                suggested_opts = [
                    "Вот свежий пример",
                    "Это происходит каждый день",
                    "Не знаю, с чего начать",
                ]
        else:
            if language == "en":
                greet = (f"Hi, {name}! " if name else "Hi! ") + screening.TRANSITION_TO_DISCOVERY["en"]
                suggested_opts = [
                    "I'll tell you about my business",
                    "I have a specific problem",
                    "I don't know what I want — let's figure it out",
                ]
            else:
                greet = (
                    f"Здравствуйте, {name}! " if name else "Здравствуйте! "
                ) + screening.TRANSITION_TO_DISCOVERY["ru"]
                suggested_opts = [
                    "Расскажу про свой бизнес",
                    "У меня есть конкретная проблема",
                    "Не знаю, чего хочу — давайте разбираться",
                ]
        session.append("assistant", greet, meta={
            "phase": "point_a",
            "kind": "welcome_after_intake",
            "suggested_options": suggested_opts,
            "internal_rationale": "Web intake-форма заполнила profile — screening пропускаем, сразу discovery",
        })
        _warm_cache[session.id] = session
        _get_store().save(session, telegram_chat_id=telegram_chat_id)
        _trigger_sheets_sync()
        return session

    # Telegram-режим: screening с нуля.
    session.state.conversation.phase = Phase.SCREENING
    session.state.conversation.screening_step = 0
    opener = screening.SCREENING_INTRO[language] + screening.format_question(0, lang=language)
    q0 = screening.SCREENING_QUESTIONS[language][0]
    session.append("assistant", opener, meta={
        "phase": "screening",
        "target_field": q0["field"],
        "target_section": "Profile",
        "kind": "screening_question",
        "internal_rationale": "screening Q1 — детерминированный опросник, без LLM",
    })
    _warm_cache[session.id] = session
    _get_store().save(session, telegram_chat_id=telegram_chat_id)
    _trigger_sheets_sync()
    return session


def get_session(sid: str) -> WorkflowSession | None:
    if sid in _warm_cache:
        return _warm_cache[sid]
    s = _get_store().load(sid)
    if s is not None:
        _warm_cache[sid] = s
    return s


def get_session_by_telegram_chat(chat_id: int) -> WorkflowSession | None:
    s = _get_store().find_by_telegram_chat(chat_id)
    if s is not None:
        _warm_cache[s.id] = s
    return s


def reply(
    session: WorkflowSession,
    user_message: str,
    *,
    telegram_chat_id: int | None = None,
    telegram_username: str | None = None,
    external_comment: str | None = None,
    from_voice: bool = False,
) -> str:
    """Один ход диалога.

    `external_comment` — отдельно поданный комментарий (например, из веб-формы, где
    у владельца есть отдельное поле для мета-фидбека). Сохраняется как классификация
    `kind="comment"` ровно так же, как комментарий, выловленный из самого текста по
    префиксу «комментарий:».

    `from_voice=True` — текст пришёл из распознавания голосового сообщения. В этом
    режиме триггер «комментарий» работает и без двоеточия (STT его не вставляет).
    """
    # Зафиксируем TG-username при первом upd, если ещё не сохранён.
    if telegram_username and not session.state.profile.telegram_username:
        session.state.profile.telegram_username = telegram_username

    # Шаг 1: отрезаем встроенный «комментарий: …» из текста (детерминированно).
    cleaned_message, inline_comment = comments.parse_comment(user_message, voice=from_voice)

    # Шаг 2: оба комментария (внешний из веб-формы и встроенный) персистим.
    turn_for_comment = max(session.state.conversation.turn_count + 1, 1)
    for raw in (external_comment, inline_comment):
        if raw and raw.strip():
            _get_store().append_classification(
                session.id,
                turn=turn_for_comment,
                kind="comment",
                text=raw.strip(),
                rationale="user-provided feedback (UI comment field or 'комментарий:' prefix)",
            )

    # Шаг 3: если в сообщении был только комментарий и больше ничего — стейт не
    # двигаем, возвращаем нейтральное подтверждение и тот же последний вопрос бота.
    if not cleaned_message.strip():
        _get_store().save(session, telegram_chat_id=telegram_chat_id)
        _trigger_sheets_sync()
        last_q = next(
            (m["content"] for m in reversed(session.transcript) if m["role"] == "assistant"),
            "",
        )
        ack = "Спасибо, записала комментарий — продолжим."
        return f"{ack}\n\n{last_q}" if last_q else ack

    if session.state.conversation.phase == Phase.SCREENING:
        text = _handle_screening_turn(session, cleaned_message)
        _get_store().save(session, telegram_chat_id=telegram_chat_id)
        _trigger_sheets_sync()
        return text

    return _handle_workflow_turn(
        session, cleaned_message, telegram_chat_id=telegram_chat_id,
    )


def _handle_screening_turn(session: WorkflowSession, user_message: str) -> str:
    """Process one screening answer. No LLM. Persist comment if any."""
    lang = session.state.conversation.language
    step = session.state.conversation.screening_step
    field_value, comment = screening.parse_input(user_message, step, lang=lang)

    q = screening.SCREENING_QUESTIONS[lang][step]
    qtype = q.get("type", "choice")

    # Mark this user message in the transcript regardless of validity.
    session.transcript.append({"role": "user", "content": user_message})

    # ---- email-специфика: невалидный email → НЕ продвигаем шаг, переспрашиваем ----
    if qtype == "email" and field_value is None:
        # Сохраним попытку как kind="comment" для отладки.
        if comment:
            _get_store().append_classification(
                session.id,
                turn=step + 1,
                kind="comment",
                text=comment,
                rationale=f"screening Q{step + 1} (email) — invalid format",
            )
        retry_msg = {
            "ru": (
                "Похоже, в email опечатка — нужен формат `имя@домен.зона`. "
                "Попробуйте ещё раз?"
            ),
            "en": (
                "Looks like a typo in the email — needs the format `name@domain.zone`. "
                "Try again?"
            ),
        }[lang]
        session.append("assistant", retry_msg, meta={
            "phase": "screening",
            "target_field": "email",
            "target_section": "Profile",
            "kind": "screening_question_retry",
            "internal_rationale": "email не прошёл regex — переспрашиваем тот же шаг",
        })
        return retry_msg

    if field_value is not None:
        setattr(session.state.profile, q["field"], field_value)

    # Persist the comment (if any) to Firestore as a classification row.
    if comment and comment.strip():
        _get_store().append_classification(
            session.id,
            turn=step + 1,
            kind="comment",
            text=comment,
            rationale=f"screening Q{step + 1} ({q['field']}) free-text comment",
        )
    # And the canonical answer as a classification.
    if field_value is not None:
        _get_store().append_classification(
            session.id,
            turn=step + 1,
            kind="answer",
            text=field_value,
            rationale=f"screening Q{step + 1} ({q['field']}) {qtype}",
        )

    # Advance step and decide what to emit next.
    session.state.conversation.screening_step = step + 1
    if screening.is_done(session.state.conversation.screening_step):
        session.state.conversation.phase = Phase.POINT_A
        out = screening.TRANSITION_TO_DISCOVERY[lang]
        meta = {
            "phase": "screening→point_a",
            "kind": "screening_transition",
            "internal_rationale": "screening завершён, открытый вопрос-приглашение в discovery",
        }
    else:
        nxt = session.state.conversation.screening_step
        nxt_q = screening.SCREENING_QUESTIONS[lang][nxt]
        out = screening.format_question(nxt, lang=lang)
        meta = {
            "phase": "screening",
            "target_field": nxt_q["field"],
            "target_section": "Profile",
            "kind": "screening_question",
            "internal_rationale": f"screening Q{nxt + 1} ({nxt_q['field']}) — детерминированный опросник, без LLM",
        }
    session.append("assistant", out, meta=meta)
    return out


def _handle_workflow_turn(
    session: WorkflowSession,
    user_message: str,
    *,
    telegram_chat_id: int | None,
) -> str:
    wf = _get_workflow()
    last_assistant = _last_role(session, "assistant")

    answer_part = user_message
    comment_part: str | None = None
    rationale = ""
    try:
        verdict = classifier.split(
            _llm or wf.llm,
            last_assistant=last_assistant,
            last_user=user_message,
        )
        rationale = verdict["rationale"]
        if verdict["answer_text"] is not None:
            answer_part = verdict["answer_text"]
        comment_part = verdict["comment_text"]
    except Exception:  # noqa: BLE001 — splitter must never break the chat
        pass

    turn = session.state.conversation.turn_count + 1
    if answer_part and answer_part.strip():
        _get_store().append_classification(
            session.id, turn=turn, kind="answer", text=answer_part, rationale=rationale,
        )
    if comment_part and comment_part.strip():
        _get_store().append_classification(
            session.id, turn=turn, kind="comment", text=comment_part, rationale=rationale,
        )

    text = wf.respond(session, answer_part)
    _get_store().save(session, telegram_chat_id=telegram_chat_id)
    _trigger_sheets_sync()
    return text


def undo(session: WorkflowSession, target_step: int) -> str:
    """Rewind session to target_step. Returns the bot question at that step.

    Each assistant message in the transcript corresponds to one "step".
    target_step is 1-based: step 1 = first assistant message.
    We keep the target step's assistant message but remove any user answer
    that follows it and everything after.
    """
    # Count assistant messages and find the transcript index of the Nth one.
    assistant_indices = [
        i for i, m in enumerate(session.transcript) if m["role"] == "assistant"
    ]
    if target_step < 1 or target_step > len(assistant_indices):
        raise ValueError(
            f"target_step {target_step} out of range (1..{len(assistant_indices)})"
        )

    cut_idx = assistant_indices[target_step - 1]
    # Keep everything up to and including the target assistant message.
    session.transcript = session.transcript[: cut_idx + 1]

    # After trimming, restore state from snapshot if available.
    last_assistant_meta = (
        session.transcript[-1].get("meta") or {}
    ) if session.transcript else {}
    snapshot = last_assistant_meta.get("_state_snapshot")
    if snapshot:
        # Reconstruct state from the snapshot taken at the time of that question.
        session.state = WorkflowState(**snapshot)
    else:
        # Fallback: old sessions without snapshots — reset turn_count and phase only.
        session.state.conversation.turn_count = len(session.transcript)

        phase_str = last_assistant_meta.get("phase")
        if phase_str:
            canonical = phase_str.split("→")[-1]
            try:
                session.state.conversation.phase = Phase(canonical)
            except ValueError:
                pass  # unknown phase string — leave phase unchanged

        # Reset confirmed_summary so the workflow re-asks if needed.
        session.state.conversation.confirmed_summary = False

    # Persist the rewound session.
    _get_store().save(session)
    _trigger_sheets_sync()

    # Return the content of the last assistant message (the question being re-asked).
    return session.transcript[-1]["content"] if session.transcript else ""


def is_configured() -> bool:
    """True if any LLM backend is wired up.

    Backends (any of these is enough):
    - ANTHROPIC_API_KEY → direct Anthropic API
    - LLM_BACKEND=gemini + GCP project → Gemini on Vertex
    - LLM_BACKEND=vertex + GCP project → Anthropic Claude on Vertex
    - VERTEX_PROJECT_ID or GCP_PROJECT_ID set → auto-pick Gemini on Vertex
    """
    backend = (os.environ.get("LLM_BACKEND") or "").strip().lower()
    if backend in ("gemini", "vertex"):
        return bool(
            os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID")
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("GCP_PROJECT_ID"):
        # Auto-pick path — Gemini will be used by get_default_llm.
        return True
    return False


def _last_role(session: WorkflowSession, role: str) -> str:
    for m in reversed(session.transcript):
        if m["role"] == role:
            return m["content"]
    return ""
