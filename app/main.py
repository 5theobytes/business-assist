"""FastAPI entrypoint for Business Assist."""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

from . import bootstrap  # noqa: E402, F401  side-effect: GCP creds
from . import agent, captcha, telegram_bot  # noqa: E402
from .rate_limit import rate_limit  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = REPO_ROOT / "static"

log = logging.getLogger("main")


def _maybe_register_telegram_webhook() -> None:
    """Auto-register Telegram webhook on startup if creds + public URL are set.

    Render injects RENDER_EXTERNAL_URL automatically; PUBLIC_URL is a fallback
    for other hosting. Without either, we skip silently (locally / in tests).
    Errors never propagate — startup must not fail on a webhook hiccup.
    """
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return
    public_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_URL")
    if not public_url:
        log.warning("auto-webhook: no RENDER_EXTERNAL_URL or PUBLIC_URL — skipping")
        return
    webhook_url = f"{public_url.rstrip('/')}/telegram/webhook"
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    try:
        result = telegram_bot.set_webhook(webhook_url, secret_token=secret)
        log.info("auto-webhook registered %s -> %s", webhook_url, result)
    except Exception:
        log.exception("auto-webhook registration failed for %s", webhook_url)


def _trigger_startup_sheets_sync() -> None:
    """Schedule a Sheets refresh shortly after every deploy.

    Firestore is the primary store and Sheets is an optional mirror. A sync
    after startup refreshes the mirror after a deployment. The debounced
    scheduler keeps startup non-blocking and avoids concurrent syncs.
    """
    try:
        from . import sheets_sync_runtime
        if not sheets_sync_runtime.is_enabled():
            return
        sheets_sync_runtime.schedule_sync(debounce_seconds=5)
        log.info("startup sheets sync scheduled (+5s)")
    except Exception:
        log.exception("startup sheets sync trigger failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _maybe_register_telegram_webhook()
    _trigger_startup_sheets_sync()
    yield


app = FastAPI(title="Business Assist", version="0.4.0", lifespan=lifespan)


class StartResponse(BaseModel):
    session_id: str
    greeting: str
    workflow: str
    language: Literal["ru", "en"] = "ru"


class ChatRequest(BaseModel):
    session_id: str
    message: str
    comment: str | None = None  # отдельное поле мета-фидбека из веб-формы


class ChatResponse(BaseModel):
    reply: str
    done: bool
    # True when the bot is awaiting an explicit confirmation/correction at
    # SPEC_REVIEW. Frontend renders a «да, вопросов нет» button so the user
    # can confirm without typing. Free-form text input still works.
    awaiting_confirm: bool = False
    # Set on the final (done=True) turn so the front-end can render a real
    # «Перейти в чат для согласования выполнения» CTA. Sourced from env
    # PAYMENT_CTA_URL — the operator configures the destination URL. Until then
    # it stays None and the front-end shows a disabled placeholder.
    payment_cta_url: str | None = None
    # ANALYSIS phase: when the bot just rendered solution options, expose the
    # short labels (1-3 entries) so the front-end can show one button per option.
    # Empty list when not on the options-rendering turn.
    analysis_options: list[dict] = Field(default_factory=list)
    # Journey Mode: clarity scores per section (point_a, point_b, resources), 0-100 each.
    clarity_score: dict | None = None
    # True if this response is a checkpoint requiring confirmation.
    is_checkpoint: bool = False
    # Suggested answer variants (quick-reply buttons) for the current question.
    suggested_options: list[str] = Field(default_factory=list)
    # Always True for now — user can always type a custom answer.
    allow_custom_input: bool = True
    # Phase marker (e.g. "point_a", "analysis", "spec_review").
    phase_marker: str | None = None


class SessionStateResponse(BaseModel):
    session_id: str
    transcript: list[dict]
    done: bool
    # Mirrors ChatResponse.payment_cta_url so a returning user can render the same CTA.
    payment_cta_url: str | None = None
    language: Literal["ru", "en"] = "ru"
    # Output documents (populated once session is done)
    clarity_doc: str | None = None
    business_spec: str | None = None
    dev_spec: str | None = None
    # Suggested options from the last assistant message, for hydration.
    last_suggested_options: list[str] = Field(default_factory=list)
    # Current Journey phase so a reloaded page can restore its progress bar.
    phase_marker: str | None = None


class StartRequest(BaseModel):
    language: Literal["ru", "en"] | None = None


class IntakeRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    email: str = Field(..., min_length=3, max_length=240)
    age_range: Literal["<25", "25-35", "35-45", "45-55", "55+"]
    gender: Literal["female", "male", "not_specified"]
    sector: Literal[
        "services", "goods", "online_education", "production", "b2b", "other",
    ]
    time_eater: Literal[
        "client_comms", "sales", "production", "admin", "marketing", "other",
    ]
    turnstile_token: str | None = None
    language: Literal["ru", "en"] | None = None


class IntakeResponse(BaseModel):
    session_id: str
    chat_url: str


class ConfigResponse(BaseModel):
    turnstile_site_key: str


class UndoRequest(BaseModel):
    session_id: str
    target_step: int  # 1-based step index to rewind to (keeps this step's question, removes answer and everything after)


class UndoResponse(BaseModel):
    success: bool
    reply: str  # the bot question at the target step (re-rendered)
    suggested_options: list[str] = Field(default_factory=list)
    phase_marker: str | None = None
    clarity_score: dict | None = None


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def detect_language(accept_language: str | None = Header(default=None)) -> Literal["ru", "en"]:
    """Default-language detector from Accept-Language header.

    Sums q-weights for `en-*` and `ru-*` tags. If en > ru → "en", else "ru".
    Bare tag (no q) defaults to weight 1.0. Empty / missing header → "ru".
    """
    if not accept_language:
        return "ru"
    en_weight = 0.0
    ru_weight = 0.0
    for entry in accept_language.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(";")
        tag = parts[0].strip().lower()
        q = 1.0
        for p in parts[1:]:
            p = p.strip()
            if p.startswith("q="):
                try:
                    q = float(p[2:])
                except ValueError:
                    q = 0.0
        if tag.startswith("en"):
            en_weight = max(en_weight, q)
        elif tag.startswith("ru"):
            ru_weight = max(ru_weight, q)
    return "en" if en_weight > ru_weight else "ru"


GREETING_FALLBACK: dict[str, str] = {
    "ru": (
        "Привет! Я помогу разобраться, что вашему бизнесу нужно автоматизировать или построить, "
        "и в конце соберу понятный план с конкретными шагами и расходами.\n\n"
        "Сейчас сервер не настроен (нет ANTHROPIC_API_KEY) — диалог не запустится, "
        "но интерфейс работает."
    ),
    "en": (
        "Hi! I'll help figure out what your business should automate or build, "
        "and at the end put together a clear plan with concrete steps and costs.\n\n"
        "The server is not configured right now (no API key) — the dialogue won't start, "
        "but the interface works."
    ),
}

FRIENDLY_RATE_LIMIT: dict[str, str] = {
    "ru": (
        "Сейчас слишком много запросов — это лимит платформы, "
        "не ваша вина. Попробуйте, пожалуйста, через минуту."
    ),
    "en": (
        "Too many requests right now — this is a platform-side limit, not your fault. "
        "Please try again in a minute."
    ),
}

FRIENDLY_GENERIC: dict[str, str] = {
    "ru": (
        "Что-то сломалось на нашей стороне. Попробуйте ещё раз через минуту, "
        "если повторится — напишите в поддержку."
    ),
    "en": (
        "Something broke on our side. Please try again in a minute; "
        "if it repeats, contact support."
    ),
}


@app.post("/api/session", response_model=StartResponse)
def start_session(
    req: StartRequest | None = None,
    lang: Literal["ru", "en"] = Depends(detect_language),
) -> StartResponse:
    effective_lang: Literal["ru", "en"] = (req.language if req and req.language else lang)
    if not agent.is_configured():
        return StartResponse(
            session_id="unconfigured",
            greeting=GREETING_FALLBACK[effective_lang],
            workflow=agent.DEFAULT_WORKFLOW,
            language=effective_lang,
        )
    session = agent.create_session(language=effective_lang)
    greeting = session.transcript[0]["content"] if session.transcript else ""
    return StartResponse(
        session_id=session.id,
        greeting=greeting,
        workflow=agent.DEFAULT_WORKFLOW,
        language=effective_lang,
    )


@app.post(
    "/api/chat",
    response_model=ChatResponse,
    dependencies=[Depends(rate_limit(limit=30, window_s=60))],
)
def chat(req: ChatRequest) -> ChatResponse:
    if not agent.is_configured():
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")
    session = agent.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        text = agent.reply(session, req.message, external_comment=req.comment)
    except Exception as exc:  # noqa: BLE001 — surface a friendly reply, not 500
        # A common deployed cause is an Anthropic 429 after retries are exhausted.
        # Don't let the chat client see a generic 500 — keep the session alive
        # and give a friendly message in the session language so the user can retry.
        from anthropic import RateLimitError
        log.exception("agent.reply failed for session %s", req.session_id)
        _lang = session.state.conversation.language
        if isinstance(exc, RateLimitError):
            friendly = FRIENDLY_RATE_LIMIT[_lang]
        else:
            friendly = FRIENDLY_GENERIC[_lang]
        return ChatResponse(reply=friendly, done=False)
    phase_val = session.state.conversation.phase.value
    is_done = phase_val == "done"

    last_meta = (
        session.transcript[-1].get("meta") or {}
    ) if session.transcript else {}
    suggested_options = last_meta.get("suggested_options") or []
    phase_marker = last_meta.get("phase")

    # Surface ANALYSIS options for the front-end ONLY on the turn where they
    # were just rendered (kind=analysis_options or _reemit). On gap-fill / lock /
    # answer turns the options array stays empty so the UI doesn't show stale
    # buttons.
    analysis_options: list[dict] = []
    if phase_val == "analysis":
        if last_meta.get("kind") in ("analysis_options", "analysis_options_reemit"):
            for i, opt in enumerate(session.state.solution.offered_options[:3], start=1):
                shape = (opt.get("shape") or f"вариант {i}").strip()
                analysis_options.append({"index": i, "shape": shape})

    return ChatResponse(
        reply=text,
        done=is_done,
        awaiting_confirm=phase_val == "spec_review",
        payment_cta_url=(os.environ.get("PAYMENT_CTA_URL") or None) if is_done else None,
        analysis_options=analysis_options,
        clarity_score=session.state.solution.clarity_score,
        is_checkpoint=(
            last_meta.get("kind") == "clarity_checkpoint"
        ),
        suggested_options=suggested_options,
        allow_custom_input=True,
        phase_marker=phase_marker,
    )


@app.post("/api/chat/undo", response_model=UndoResponse)
def chat_undo(req: UndoRequest) -> UndoResponse:
    if not agent.is_configured():
        raise HTTPException(status_code=503, detail="Not configured")
    session = agent.get_session(req.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.state.conversation.phase.value == "done":
        raise HTTPException(status_code=400, detail="Cannot undo completed session")
    try:
        reply_text = agent.undo(session, req.target_step)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    last_meta = (session.transcript[-1].get("meta") or {}) if session.transcript else {}
    return UndoResponse(
        success=True,
        reply=reply_text,
        suggested_options=last_meta.get("suggested_options") or [],
        phase_marker=last_meta.get("phase"),
        clarity_score=session.state.solution.clarity_score,
    )


@app.post(
    "/api/intake",
    response_model=IntakeResponse,
    dependencies=[Depends(rate_limit(limit=5, window_s=60))],
)
def intake(
    request: Request,
    req: IntakeRequest,
    lang: Literal["ru", "en"] = Depends(detect_language),
) -> IntakeResponse:
    """Веб-форма опроса: создаёт сессию с заполненным профилем и отдаёт ссылку на /chat."""
    if not agent.is_configured():
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not configured")

    # Дублируем email-валидацию на сервере, не доверяя фронту.
    if not _EMAIL_RE.match(req.email.strip()):
        raise HTTPException(status_code=400, detail="неправильный формат email")

    # Captcha (если включена в env).
    client_ip = request.client.host if request.client else None
    if not captcha.verify_turnstile(req.turnstile_token, remoteip=client_ip):
        raise HTTPException(
            status_code=400,
            detail="не удалось пройти проверку безопасности, обновите страницу",
        )

    effective_lang: Literal["ru", "en"] = req.language if req.language else lang
    session = agent.create_session(
        prefilled_profile={
            "name": req.name.strip(),
            "email": req.email.strip(),
            "age_range": req.age_range,
            "gender": req.gender,
            "sector": req.sector,
            "time_eater": req.time_eater,
        },
        language=effective_lang,
    )
    return IntakeResponse(
        session_id=session.id,
        chat_url=f"/chat?sid={session.id}",
    )


@app.get("/api/config", response_model=ConfigResponse)
def get_config() -> ConfigResponse:
    """Публичный конфиг для фронта (только то, что не секретно)."""
    return ConfigResponse(turnstile_site_key=captcha.site_key())


@app.get("/api/session/{sid}", response_model=SessionStateResponse)
def get_session_state(sid: str) -> SessionStateResponse:
    """Возвращает transcript существующей сессии — для гидратации chat.html
    после редиректа из /intake (или возврата по прямой ссылке /chat?sid=...)."""
    session = agent.get_session(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # Отдаём только role + content (meta — внутренние мысли, не для UI).
    transcript = [
        {"role": m["role"], "content": m["content"]} for m in session.transcript
    ]
    is_done = session.state.conversation.phase.value == "done"
    # Extract suggested_options from last assistant message meta.
    last_assistant_meta: dict = {}
    for m in reversed(session.transcript):
        if m.get("role") == "assistant":
            last_assistant_meta = m.get("meta") or {}
            break
    last_suggested_options = last_assistant_meta.get("suggested_options") or []
    phase_marker = (
        last_assistant_meta.get("phase")
        or session.state.conversation.phase.value
    )
    return SessionStateResponse(
        session_id=session.id,
        transcript=transcript,
        done=is_done,
        payment_cta_url=(os.environ.get("PAYMENT_CTA_URL") or None) if is_done else None,
        language=session.state.conversation.language,
        clarity_doc=session.clarity_doc if is_done else None,
        business_spec=session.business_spec if is_done else None,
        dev_spec=session.dev_spec if is_done else None,
        last_suggested_options=last_suggested_options,
        phase_marker=phase_marker,
    )


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        raise HTTPException(status_code=401, detail="bad webhook secret")
    update = await request.json()
    background.add_task(telegram_bot.handle_update, update)
    return JSONResponse({"ok": True})


@app.post("/telegram/admin/set-webhook")
def admin_set_webhook(
    public_url: str,
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or admin_token != expected:
        raise HTTPException(status_code=401, detail="admin token required")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    return telegram_bot.set_webhook(public_url, secret_token=secret)


@app.post("/telegram/admin/sync")
def admin_sync(
    admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> dict:
    """Ручной триггер полного sync Firestore → Google Sheets.

    Полезен для cron-сервиса (GitHub Actions, cron-job.org) или ручной
    подсветки таблицы. Защищён ADMIN_TOKEN.
    """
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or admin_token != expected:
        raise HTTPException(status_code=401, detail="admin token required")
    from .sheets_sync_runtime import run_sync_inline
    return run_sync_inline()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "model": agent.MODEL,
        "workflow": agent.DEFAULT_WORKFLOW,
        "configured": agent.is_configured(),
    }


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> RedirectResponse:
    """Open the tool's intake flow at the application root."""
    return RedirectResponse(url="/intake")


@app.get("/chat")
def chat_page() -> FileResponse:
    """Чат-окно. Ожидает ?sid=<session_id> в query, иначе app.js редиректит на /intake."""
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/journey")
def journey_page() -> FileResponse:
    """Journey Mode — horizontal path visualisation of the conversation."""
    return FileResponse(STATIC_DIR / "journey.html")


@app.get("/intake")
def intake_page() -> FileResponse:
    """Форма опроса перед чатом."""
    return FileResponse(STATIC_DIR / "intake.html")
