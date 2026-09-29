"""Canonical conversation state used by V2 and V3 workflows.

Kept as a plain pydantic model so we can roundtrip it through Anthropic tool use
(the JSON schema is derived from this). All fields are optional so the extractor
can update them incrementally — `coverage()` reports how complete each section is.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Phase(str, Enum):
    GREETING = "greeting"
    SCREENING = "screening"   # 4 fixed survey questions before workflow proper (see app/screening.py)
    POINT_A = "point_a"
    POINT_B = "point_b"
    ANALYSIS = "analysis"     # bot proposes 2–3 solution shapes; owner picks one before RESOURCES
    RESOURCES = "resources"
    DEV_COVERAGE = "dev_coverage"  # spec-writer scans for gaps and asks targeted follow-ups
    SPEC_REVIEW = "spec_review"
    DONE = "done"


class Profile(BaseModel):
    """Owner profile collected in screening (intake form on web, sequential
    questions in Telegram).

    Captured deterministically from scripted multi-choice answers + free-text
    fields (name, email), not extracted by the LLM — survives prompt drift,
    lands in state precisely as keyed.
    """

    name: str | None = Field(None, description="Как обращаться к владельцу")
    email: str | None = Field(None, description="Email для финального уведомления и связи")
    telegram_username: str | None = Field(
        None, description="Telegram @username без знака @ — для связи через бота",
    )
    age_range: str | None = Field(None, description="<25 | 25-35 | 35-45 | 45-55 | 55+")
    gender: str | None = Field(None, description="female | male | not_specified")
    sector: str | None = Field(
        None,
        description="services | goods | online_education | production | b2b | other",
    )
    time_eater: str | None = Field(
        None,
        description="client_comms | sales | production | admin | marketing | other",
    )


class PointA(BaseModel):
    business_type: str | None = Field(None, description="Чем занимается бизнес")
    stage: str | None = Field(None, description="Стадия: только начинает / работает / масштабируется")
    team_size: int | None = Field(None, description="Сколько людей в команде, включая владельца")
    channels: list[str] = Field(default_factory=list, description="Каналы клиентов: telegram, instagram, сайт, ...")
    tools_in_use: list[str] = Field(default_factory=list, description="Текущие инструменты: Excel, AmoCRM, Tilda, ...")
    pain_points: list[str] = Field(
        default_factory=list, description="Болевые точки с метрикой (частота / время)"
    )
    daily_volume: str | None = Field(None, description="Сколько обращений/заказов в день")


class PointB(BaseModel):
    primary_value: str | None = Field(
        None, description="time | money | sanity | customer_experience"
    )
    desired_outcome: str | None = Field(None, description="Конкретное наблюдаемое 'было → стало'")
    success_metric: str | None = Field(None, description="Как поймём что получилось (число/факт)")
    out_of_scope: list[str] = Field(default_factory=list, description="Что точно НЕ делаем")
    timeframe: str | None = Field(None, description="Срок реализации")
    audience: str | None = Field(None, description="Кто будет пользоваться результатом")
    user_flows: list[str] = Field(
        default_factory=list,
        description="Сценарии 'клиент делает X — система делает Y' бытовыми словами",
    )
    functional_requirements_owner: list[str] = Field(
        default_factory=list,
        description="Что система обязательно должна уметь, фразами владельца — LLM формализует в FR-N",
    )


class Resources(BaseModel):
    monthly_budget_rub: int | None = Field(
        None,
        description=(
            "Owner's stated monthly budget for tools/subscriptions, stored as a "
            "plain integer. CURRENCY IS NOT ENCODED in this value — the legacy "
            "'_rub' suffix is kept for Firestore compatibility but the field is "
            "language-agnostic. The render layer adds $ or ₽ based on session "
            "language. STORE THE NUMBER AS-IS in the currency the owner used: "
            "if they say '$40-60' store the upper bound 60; if they say '5000 ₽' "
            "store 5000. NEVER multiply by an exchange rate. NEVER convert USD "
            "to RUB or vice versa. Just the number, in their own unit."
        ),
    )
    has_llm_key: bool | None = Field(None, description="Есть ли API-ключ к LLM")
    llm_provider: str | None = Field(None, description="OpenAI / Anthropic / Yandex / GigaChat / другое")
    llm_subscription_quote: str | None = Field(
        None,
        description=(
            "Дословная фраза владельца про наличие/отсутствие подписки на LLM "
            "(ChatGPT/Claude/Gemini/etc). Заполняется только из ответа владельца "
            "на вопрос про has_llm_key/llm_provider. Хранится verbatim — postprocess "
            "не правит, не сокращает. Если None — строка про ИИ в Сводке не "
            "рендерится (никаких шаблонных «уже подключено / подключим в процессе»)."
        ),
    )
    integrations_available: list[str] = Field(
        default_factory=list, description="Уже есть аккаунты/доступы в этих сервисах"
    )
    integrations_needed: list[str] = Field(
        default_factory=list, description="Сервисы которые нужны но ещё не подключены"
    )
    tech_savviness: str | None = Field(
        None, description="none | basic | confident — техническая опытность пользователя"
    )
    maintainer: str | None = Field(
        None,
        description=(
            "Who maintains the system after launch. Stored as the owner's "
            "VERBATIM PHRASE in their own language — e.g. 'I'll maintain it "
            "myself', 'мой ассистент Лена', 'we'll hand it off to our IT guy', "
            "'наш разработчик Игорь'. Do NOT canonicalize to a short token like "
            "'user'/'owner'/'team'/'external'. Preserve the owner's exact words."
        ),
    )
    deployment_constraints: list[str] = Field(
        default_factory=list,
        description="Уже-используемое или явно нежелаемое: 'сайт на Tilda', 'не хочу платный сервер'",
    )
    preferred_channel: str | None = Field(
        None,
        description="Где удобно клиенту общаться: site_chat | telegram | both | other",
    )
    dev_coverage_qa: list[dict] = Field(
        default_factory=list,
        description=(
            "Q+A pairs collected during DEV_COVERAGE phase. Each entry is "
            "{'question': str, 'answer': str, 'rationale': str}. The spec-writer "
            "scanner LLM generates questions to close gaps before the dev spec is "
            "drafted; owner answers go here. Read by the dev-spec writer to lock "
            "case-specific decisions (interface details, channel fallbacks, "
            "human-in-the-loop rules, data sources, resource trade-offs)."
        ),
    )
    llm_required: bool | None = Field(
        None,
        description=(
            "Set by _detect_llm_requirement once point_b.desired_outcome is filled. "
            "True if solution needs natural-language understanding/generation "
            "(chatbot, auto-replies, content gen, NL Q&A). False if pure rules / "
            "sync / routing (forward order A→B, scheduled reminders, dedup). "
            "When False: skip ChatGPT-subscription question, hide «ИИ для бота» "
            "row in summary. Include AI only when it supports the user's stated need."
        ),
    )


class Solution(BaseModel):
    """Зафиксированный выбор владельца про форму решения. Заполняется на
    фазе ANALYSIS. Гейтит часть RESOURCES-вопросов и Сводку.

    `shape is None` означает «выбор ещё не сделан» — фаза ANALYSIS не завершена.
    """

    shape: str | None = Field(
        None,
        description=(
            "Короткое каноническое имя выбранной формы: «бот в TG» / «sync без бота» / "
            "«чат на сайте» / «график в google-таблице» / «связка двух сервисов». "
            "None — выбор ещё не сделан, ANALYSIS не завершён."
        ),
    )
    shape_description: str | None = Field(
        None,
        description=(
            "1–2 предложения дословно из той опции, что выбрал владелец. "
            "Идёт в Сводку и в дев-спеку как ground truth «что договорились строить»."
        ),
    )
    needs_ai: bool | None = Field(
        None,
        description=(
            "True если выбранная форма требует LLM (понимание/генерация языка). "
            "False если только правила/sync/расписание/UI. None пока выбор не сделан. "
            "Источник истины для resources.llm_required и для рендера ИИ-строки в Сводке."
        ),
    )
    needs_chat_ui: bool | None = Field(
        None,
        description=(
            "True если форма содержит чат-интерфейс с конечным клиентом (TG-бот, "
            "виджет на сайте). Гейтит вопрос про preferred_channel."
        ),
    )
    needs_external_integrations: bool | None = Field(
        None,
        description=(
            "True если форма требует подключения внешних сервисов (CRM, мессенджеры, "
            "аналитику). Гейтит вопрос про integrations_needed."
        ),
    )
    analysis_qa: list[dict] = Field(
        default_factory=list,
        description=(
            "Q+A пары gap-fill подэтапа. Структура: "
            '{"question": str, "answer": str, "gap": str}. '
            "Читает options-LLM как доп-контекст к A+B."
        ),
    )
    offered_options: list[dict] = Field(
        default_factory=list,
        description=(
            "Последний набор опций, которые бот показал владельцу (для повторного "
            "показа после question/correction). Каждая опция: "
            '{"shape": str, "description": str, "needs_ai": bool, "needs_chat_ui": bool, '
            '"needs_external_integrations": bool, "approx_budget_rub": str, '
            '"approx_timeline": str, "recommended": bool}.'
        ),
    )
    decision_log: list[dict] = Field(
        default_factory=list,
        description=(
            "V4 clarity workflow: log of every substantive decision point. Each entry: "
            '{"question": str, "answer": str, "alternatives": [str], "rationale": str}. '
            "Populated by the extractor's log_decision tool call."
        ),
    )
    clarity_score: dict = Field(
        default_factory=lambda: {"point_a": 0, "point_b": 0, "resources": 0},
        description=(
            "V4 clarity workflow: running clarity scores per section (0-100). "
            "Updated after each extraction pass."
        ),
    )


class SessionMemory(BaseModel):
    """Session-level retraction & preference store.

    Users may retract facts or state preferences during a conversation. This
    memory records those changes so prompts do not reintroduce removed facts
    and a postprocess validator can reject contradictory output.
    """

    retractions: list[dict] = Field(
        default_factory=list,
        description=(
            "User-reported removals. Each entry: "
            '{"type": "channel"|"tool"|"feature"|"resource", '
            '"value": "<canonical name>", "turn": <int>, '
            '"user_quote": "<verbatim sentence>"}'
        ),
    )
    preferences: list[dict] = Field(
        default_factory=list,
        description=(
            "User-reported constraints and preferences. Each entry: "
            '{"topic": "<no_ai|budget_strict|simplicity|...>", '
            '"value": "<short value>", "turn": <int>, '
            '"user_quote": "<verbatim sentence>"}'
        ),
    )


class Conversation(BaseModel):
    """Conversation-level metadata used by V2/V3 to drive transitions."""

    phase: Phase = Phase.GREETING
    turn_count: int = 0
    language: Literal["ru", "en"] = Field(
        default="ru",
        description=(
            "User's chosen language for the website session. Set at session creation "
            "from Accept-Language header or explicit POST payload; immutable thereafter "
            "(toggle is hidden on /chat). Drives every user-facing string: asker persona, "
            "screening questions, summary template, final ack/CTA, spec-writer outputs. "
            "TG bot pins this to 'ru' regardless."
        ),
    )
    last_summary_turn: int = 0
    screening_step: int = 0  # number of completed scripted-survey questions (0..4)
    open_threads: list[str] = Field(
        default_factory=list,
        description="Темы которые пользователь поднял и куда стоит вернуться",
    )
    confirmed_summary: bool = False
    started_at: datetime | None = Field(
        None, description="UTC момент создания сессии (выставляется в agent.create_session)",
    )
    ended_at: datetime | None = Field(
        None,
        description="UTC момент финальной выдачи спек — _emit_specs ставит при первом достижении DONE",
    )
    estimate_text: str | None = Field(
        None,
        description="Грубая вилка срока реализации — короткая фраза для ack-сообщения владельцу",
    )
    dev_coverage_complete: bool = Field(
        False,
        description=(
            "Set by the DEV_COVERAGE scanner when it returns complete=true (no more "
            "gaps to ask the owner) OR when the safety cap of 8 scanner iterations "
            "is hit. Drives the next_phase transition from DEV_COVERAGE to SPEC_REVIEW."
        ),
    )
    session_memory: SessionMemory = Field(
        default_factory=SessionMemory,
        description=(
            "Per-session retractions + preferences. Filled by extractor mini-pass on "
            "every user turn that contains negation/removal/preference signals. "
            "Read by asker/scanner/PM/spec-writers as a constraint block, and by "
            "postprocess.check_against_memory before each bot reply is emitted."
        ),
    )


class WorkflowState(BaseModel):
    profile: Profile = Field(default_factory=lambda: Profile())
    point_a: PointA = Field(default_factory=lambda: PointA())
    point_b: PointB = Field(default_factory=lambda: PointB())
    solution: Solution = Field(default_factory=lambda: Solution())
    resources: Resources = Field(default_factory=lambda: Resources())
    conversation: Conversation = Field(default_factory=lambda: Conversation())

    # ---------- coverage helpers ----------

    @staticmethod
    def _ratio(model: BaseModel, required: list[str]) -> float:
        filled = 0
        for name in required:
            v = getattr(model, name, None)
            if isinstance(v, list):
                if v:
                    filled += 1
            elif v not in (None, ""):
                filled += 1
        return filled / max(1, len(required))

    def coverage_a(self) -> float:
        return self._ratio(
            self.point_a,
            ["business_type", "channels", "tools_in_use", "pain_points"],
        )

    def coverage_b(self) -> float:
        return self._ratio(
            self.point_b,
            ["primary_value", "desired_outcome", "success_metric", "out_of_scope"],
        )

    def coverage_resources(self) -> float:
        return self._ratio(
            self.resources,
            ["monthly_budget_rub", "has_llm_key", "tech_savviness", "maintainer"],
        )

    def open_gaps(self, phase: Phase) -> list[str]:
        """Return the names of fields that still need answers for the given phase."""
        if phase == Phase.POINT_A:
            return [
                f for f in ["business_type", "channels", "tools_in_use", "pain_points"]
                if not getattr(self.point_a, f) or (isinstance(getattr(self.point_a, f), list) and not getattr(self.point_a, f))
            ]
        if phase == Phase.POINT_B:
            return [
                f for f in ["primary_value", "desired_outcome", "success_metric", "out_of_scope"]
                if not getattr(self.point_b, f) or (isinstance(getattr(self.point_b, f), list) and not getattr(self.point_b, f))
            ]
        if phase == Phase.RESOURCES:
            return [
                f for f in ["monthly_budget_rub", "has_llm_key", "tech_savviness", "maintainer"]
                if getattr(self.resources, f) in (None, "")
            ]
        return []

    def is_phase_complete(self, phase: Phase, threshold: float = 0.75) -> bool:
        if phase == Phase.POINT_A:
            return self.coverage_a() >= threshold
        if phase == Phase.POINT_B:
            return self.coverage_b() >= threshold
        if phase == Phase.ANALYSIS:
            # ANALYSIS завершена когда владелец зафиксировал форму решения.
            return self.solution.shape is not None
        if phase == Phase.RESOURCES:
            return self.coverage_resources() >= threshold
        return False

    def next_phase(self) -> Phase:
        """Advance phase deterministically based on coverage."""
        cur = self.conversation.phase
        if cur == Phase.GREETING:
            return Phase.POINT_A
        if cur == Phase.POINT_A and self.is_phase_complete(Phase.POINT_A):
            return Phase.POINT_B
        if cur == Phase.POINT_B and self.is_phase_complete(Phase.POINT_B):
            return Phase.ANALYSIS
        if cur == Phase.ANALYSIS and self.is_phase_complete(Phase.ANALYSIS):
            return Phase.RESOURCES
        if cur == Phase.RESOURCES and self.is_phase_complete(Phase.RESOURCES):
            return Phase.DEV_COVERAGE
        if cur == Phase.DEV_COVERAGE and self.conversation.dev_coverage_complete:
            return Phase.SPEC_REVIEW
        if cur == Phase.SPEC_REVIEW and self.conversation.confirmed_summary:
            return Phase.DONE
        return cur
