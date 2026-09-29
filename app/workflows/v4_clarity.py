"""V4 — Clarity Workflow.

Guides from zero clarity to three structured outputs:
  Output A: Client Clarity Document (warm, second-person, no jargon)
  Output B: Developer Brief (professional, accessible)
  Output C: Technical Specification & Verification Checklist

Key differences from V2:
  - Clarity checkpoints before phase transitions (paraphrase + confirm)
  - "I don't know" scaffolding (examples, analogies, multiple-choice)
  - Decision logging (every substantive answer → decision_log)
  - Three-document generation instead of two
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .. import spec_manifest
from ..llm import LLM
from ..skills_loader import load_skill
from ..state import Phase, WorkflowState
from .base import Workflow, WorkflowSession
from .common_prompts import (
    ANALYSIS_OPTIONS_PROMPT,
    ANALYSIS_PM_PROMPT,
    ANALYSIS_SCANNER_PROMPT,
    ANSWER_USER_QUESTION_PROMPT,
    BUSINESS_SPEC_PROMPT,
    CLARITY_CHECKPOINT_PROMPT,
    CONVERSATIONAL_STYLE,
    DECISION_LOGGING_INSTRUCTION,
    DEV_SPEC_PROMPT,
    DETECT_LLM_REQUIREMENT_PROMPT,
    EXTRACT_MEMORY_PROMPT,
    FINAL_ACK_TEMPLATE,
    IDK_HANDLER_PROMPT,
    PM_REVIEW_PROMPT,
    SPEC_COVERAGE_SCAN_PROMPT,
    SUMMARY_TEMPLATE,
    get_automation_catalog,
)
from .postprocess import (
    apply_no_ai_if_rejected,
    canonicalize_list,
    check_against_memory,
    check_variant_blocks,
    filter_channels_to_user_history,
    localize_foreign_fragments,
    recover_budget_from_history,
    sanitize,
    strip_dev_mentions,
)

DEV_COVERAGE_MAX_ITERATIONS = 8
CHECKPOINT_EVERY = 4  # slightly more frequent than v2 for clarity workflow
CONFIRM_BUTTON_TEXT = "да, вопросов нет, подтверждаю"

_IDK_SIGNALS_RU = (
    "не знаю", "не уверен", "не уверена", "сложно сказать", "затрудняюсь",
    "без понятия", "хз", "не могу сказать", "не представляю", "не думал",
    "не думала", "понятия не имею", "трудно сказать",
)
_IDK_SIGNALS_EN = (
    "don't know", "not sure", "no idea", "hard to say", "can't say",
    "no clue", "haven't thought", "difficult to say", "unsure",
    "i'm not certain", "beats me",
)

_OPENER: dict[str, str] = {
    "ru": (
        "Привет! Я помогу разобраться, что вашему бизнесу нужно — шаг за шагом, "
        "без спешки. В конце у вас будет понятный план, написанный вашими словами.\n\n"
        "Я буду задавать вопросы и периодически пересказывать, что понял — "
        "чтобы ничего не потерялось. Если где-то непонятно — скажите, разберёмся вместе.\n\n"
        "С чего начнём?\n"
        "— Расскажу про свой бизнес\n"
        "— У меня есть конкретная проблема\n"
        "— Не знаю, чего хочу — давайте разбираться"
    ),
    "en": (
        "Hi! I'll help figure out what your business needs — step by step, "
        "no rush. At the end you'll have a clear plan written in your own words.\n\n"
        "I'll ask questions and periodically summarize what I understood — "
        "so nothing gets lost. If anything is unclear, just say so and we'll work through it.\n\n"
        "Where shall we start?\n"
        "— I'll tell you about my business\n"
        "— I have a specific problem\n"
        "— I don't know what I want — let's figure it out together"
    ),
}

_OPENER_OPTIONS: dict[str, list[str]] = {
    "ru": ["Расскажу про свой бизнес", "У меня есть конкретная проблема", "Не знаю, чего хочу — давайте разбираться"],
    "en": ["I'll tell you about my business", "I have a specific problem", "I don't know what I want — let's figure it out together"],
}

_CHECKPOINT_OPTIONS: dict[str, list[str]] = {
    "ru": ["Да, подтверждаю", "Есть правки"],
    "en": ["Yes, confirmed", "I have corrections"],
}

_VARIANT_GEN_PROMPT: dict[str, str] = {
    "ru": (
        "Ты генерируешь 2-4 коротких варианта ответа на вопрос бизнес-консультанта. "
        "Каждый вариант — 3-8 слов, разговорный тон. Верни JSON: {\"options\": [\"...\", ...]}\n"
        "Не повторяй вопрос. Варианты должны быть конкретными и релевантными контексту бизнеса."
    ),
    "en": (
        "Generate 2-4 short answer options for a business consultant's question. "
        "Each option: 3-8 words, conversational tone. Return JSON: {\"options\": [\"...\", ...]}\n"
        "Don't repeat the question. Options must be concrete and relevant to the business context."
    ),
}

# Extended extractor tool schema with decision logging
EXTRACTOR_TOOL = {
    "name": "update_state",
    "description": (
        "Extract facts from the user's latest message and update the structured state. "
        "Only pass fields that are genuinely new/refined from this message. "
        "Do not invent facts. If nothing new — call with empty patches."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "point_a": {
                "type": "object",
                "properties": {
                    "business_type": {"type": "string"},
                    "stage": {"type": "string"},
                    "team_size": {"type": "integer"},
                    "channels": {"type": "array", "items": {"type": "string"}},
                    "tools_in_use": {"type": "array", "items": {"type": "string"}},
                    "pain_points": {"type": "array", "items": {"type": "string"}},
                    "daily_volume": {"type": "string"},
                },
            },
            "point_b": {
                "type": "object",
                "properties": {
                    "primary_value": {
                        "type": "string",
                        "enum": ["time", "money", "sanity", "customer_experience"],
                    },
                    "desired_outcome": {"type": "string"},
                    "success_metric": {"type": "string"},
                    "out_of_scope": {"type": "array", "items": {"type": "string"}},
                    "timeframe": {"type": "string"},
                    "audience": {"type": "string"},
                    "user_flows": {"type": "array", "items": {"type": "string"}},
                    "functional_requirements_owner": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
            },
            "resources": {
                "type": "object",
                "properties": {
                    "monthly_budget_rub": {
                        "type": "integer",
                        "description": (
                            "Monthly budget for tools/subscriptions, plain integer. "
                            "Store as-is in the owner's currency. Never convert."
                        ),
                    },
                    "has_llm_key": {"type": "boolean"},
                    "llm_provider": {"type": "string"},
                    "llm_subscription_quote": {
                        "type": "string",
                        "description": "Verbatim user quote about AI subscription.",
                    },
                    "integrations_available": {"type": "array", "items": {"type": "string"}},
                    "integrations_needed": {"type": "array", "items": {"type": "string"}},
                    "tech_savviness": {
                        "type": "string",
                        "enum": ["none", "basic", "confident"],
                    },
                    "maintainer": {
                        "type": "string",
                        "description": "Verbatim owner phrase about who maintains after launch.",
                    },
                    "deployment_constraints": {"type": "array", "items": {"type": "string"}},
                    "preferred_channel": {"type": "string"},
                },
            },
            "user_confirmed_summary": {
                "type": "boolean",
                "description": "True if user confirmed the last recap",
            },
        },
        "additionalProperties": False,
    },
}

LOG_DECISION_TOOL = {
    "name": "log_decision",
    "description": (
        "Log a decision point with the question asked, the chosen answer, "
        "alternatives considered, and rationale. Call this for every substantive "
        "choice the user makes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What was asked"},
            "answer": {"type": "string", "description": "What the user chose"},
            "alternatives": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Other options that were available",
            },
            "rationale": {
                "type": "string",
                "description": "Why this choice makes sense (1 sentence)",
            },
        },
        "required": ["question", "answer"],
    },
}

_CANONICALIZED_LIST_FIELDS = {
    "channels", "tools_in_use", "integrations_available", "integrations_needed",
    "deployment_constraints",
}


def _merge_patch(state: WorkflowState, patch: dict) -> None:
    """Apply a partial update from the extractor tool to the state."""

    def assign(target, key, value):
        if value in (None, ""):
            return
        existing = getattr(target, key, None)
        if isinstance(existing, list):
            existing_lower = {
                str(x).strip().lower() for x in existing if x not in (None, "")
            }
            for item in value:
                if item in (None, ""):
                    continue
                norm = str(item).strip().lower()
                if norm and norm not in existing_lower:
                    existing.append(item)
                    existing_lower.add(norm)
            if key in _CANONICALIZED_LIST_FIELDS:
                setattr(target, key, canonicalize_list(existing))
        else:
            setattr(target, key, value)

    for section_key in ("point_a", "point_b", "resources"):
        section_patch = patch.get(section_key) or {}
        section = getattr(state, section_key)
        for k, v in section_patch.items():
            assign(section, k, v)
    if patch.get("user_confirmed_summary"):
        state.conversation.confirmed_summary = True


def _extractor_system(lang: str = "en") -> str:
    decision_instruction = DECISION_LOGGING_INSTRUCTION[lang]
    return (
        "You are a STRUCTURED-EXTRACTOR for a business discovery conversation. "
        "Read the user's latest message in context of the conversation and call "
        "`update_state` with any new facts. If the user made a substantive choice "
        "(picked one option over others, stated a preference, made a decision), "
        "ALSO call `log_decision` to record it.\n\n"
        f"Decision logging rule: {decision_instruction}\n\n"
        "If nothing new — still call `update_state` with empty patches. "
        "Do NOT write any text, only tool calls."
    )


def _asker_system(state: WorkflowState, lang: str = "en") -> str:
    """Build the asker system prompt with clarity-specific enhancements."""
    phase = state.conversation.phase
    next_field = spec_manifest.next_owner_field(state, phase)
    remaining = spec_manifest.owner_gaps(state, phase)
    llm_required = state.resources.llm_required

    # IDK handler instruction
    idk_instruction = IDK_HANDLER_PROMPT[lang]
    # Clarity checkpoint instruction
    clarity_instruction = CLARITY_CHECKPOINT_PROMPT[lang]

    if lang == "en":
        resources_focus = (
            "Now exploring resources — budget, access, support."
            if llm_required is not False
            else (
                "Now exploring resources — budget, access, support. "
                "This case does NOT require AI — skip ChatGPT/Claude subscription questions."
            )
        )
        phase_focus = {
            Phase.POINT_A: "Collecting Point A — how things work today. Close gaps one at a time.",
            Phase.POINT_B: "Exploring Point B — what should change. Focus on measurable outcomes.",
            Phase.RESOURCES: resources_focus,
            Phase.SPEC_REVIEW: "Final recap before the plan. Confirm key facts.",
        }.get(phase, "")

        if next_field is not None:
            if llm_required is False and next_field.name in ("has_llm_key", "llm_provider"):
                focus_hint = (
                    "\n\n## What to ask now\nThis case doesn't need AI — "
                    "move to the next resource item."
                )
            else:
                focus_hint = (
                    f"\n\n## What to ask now\n"
                    f"Target field: `{next_field.name}` (section '{next_field.section}').\n"
                    f"Suggested phrasing: '{next_field.owner_question[lang]}'"
                )
        else:
            focus_hint = "\n\n## What to ask now\nAll fields closed — move to recap."

        remaining_names = ", ".join(f.name for f in remaining) if remaining else "none"
        one_q_instruction = "Ask ONE question. Offer options when appropriate."
    else:
        resources_focus = (
            "Сейчас выясняешь ресурсы — бюджет, доступы, поддержку."
            if llm_required is not False
            else (
                "Сейчас выясняешь ресурсы — бюджет, доступы, поддержку. "
                "ИИ-сервис НЕ требуется — пропусти вопросы про подписку на ChatGPT/Claude."
            )
        )
        phase_focus = {
            Phase.POINT_A: "Собираешь Точку А — как сейчас всё устроено. По одному вопросу.",
            Phase.POINT_B: "Выясняешь Точку Б — что должно стать иначе. Метрика успеха важна.",
            Phase.RESOURCES: resources_focus,
            Phase.SPEC_REVIEW: "Финальный пересказ перед планом. Подтверди ключевое.",
        }.get(phase, "")

        if next_field is not None:
            if llm_required is False and next_field.name in ("has_llm_key", "llm_provider"):
                focus_hint = (
                    "\n\n## Что спросить сейчас\n"
                    "ИИ не нужен — перейди к следующему ресурсному пункту."
                )
            else:
                focus_hint = (
                    f"\n\n## Что спросить сейчас\n"
                    f"Цель: поле `{next_field.name}` (раздел «{next_field.section}»).\n"
                    f"Подсказка: «{next_field.owner_question[lang]}»"
                )
        else:
            focus_hint = "\n\n## Что спросить сейчас\nВсе поля закрыты — переходи к пересказу."

        remaining_names = ", ".join(f.name for f in remaining) if remaining else "нет"
        one_q_instruction = "Задай ОДИН вопрос. Предлагай варианты."

    return (
        CONVERSATIONAL_STYLE[lang]
        + _memory_constraint_block(state, lang=lang)
        + f"\n\n## Uncertainty handling\n{idk_instruction}"
        + f"\n\n## Clarity checkpoint pattern\n{clarity_instruction}"
        + "\n\n## Текущая фаза\n" + phase_focus
        + focus_hint
        + f"\n\n## Remaining gaps: {remaining_names}"
        + "\n\n## Current state (internal only):\n"
        + json.dumps(state.model_dump(mode='json'), ensure_ascii=False, indent=2)
        + f"\n\n{one_q_instruction}"
    )


def _memory_constraint_block(state: WorkflowState, lang: str = "ru") -> str:
    """Render constraint block based on session_memory."""
    memory = state.conversation.session_memory
    retractions = memory.retractions or []
    preferences = memory.preferences or []
    if not retractions and not preferences:
        return ""
    if lang == "en":
        lines = ["\n\n## Owner EXCLUDED or RESTRICTED — do NOT bring these back:"]
        for r in retractions:
            lines.append(f"- REMOVED ({r.get('type', '?')}): '{r.get('value', '')}'")
        for p in preferences:
            lines.append(f"- PREFERENCE ({p.get('topic', '?')}): {p.get('value', '')}")
        lines.append("Step over anything in this list.")
    else:
        lines = ["\n\n## Владелец ИСКЛЮЧИЛ или ОГРАНИЧИЛ — НЕ возвращай:"]
        for r in retractions:
            lines.append(f"- УБРАНО ({r.get('type', '?')}): «{r.get('value', '')}»")
        for p in preferences:
            lines.append(f"- ПРЕДПОЧТЕНИЕ ({p.get('topic', '?')}): {p.get('value', '')}")
        lines.append("Перешагивай и переходи к следующему вопросу.")
    return "\n".join(lines)


# ---- Clarity spec generation prompts ----

CLARITY_DOC_PROMPT: dict[str, str] = {
    "ru": """Ты — тёплый, внимательный копирайтер. На вход — JSON со структурированным состоянием бизнес-проекта.

Напиши документ «Ваша ясность» — от второго лица, как будто рассказываешь другу что он решил.

Структура:
1. **Где вы сейчас** — текущая ситуация (из point_a)
2. **Чего вы хотите** — желаемый результат словами клиента (из point_b)
3. **Почему это важно** — мотивация, боль, что изменится в жизни
4. **Что вы решили** — ключевые решения из decision_log. Каждое как простое утверждение: «Вы выбрали X, потому что Y»
   Если есть технические уточнения (с пометкой [Technical clarification]) — добавь раздел «Технические решения» с простыми объяснениями.
5. **Что дальше** — следующие шаги, ожидания по срокам

Правила:
- Второе лицо: «Вы хотите...», «Ваш бизнес...», «Вы решили...»
- ЗАПРЕЩЕНО: API, webhook, endpoint, SDK, LLM, MVP, backend, middleware, хостинг, спека, виджет
- Тёплый разговорный тон, как будто объясняешь другу
- Если раздел пустой — пропусти молча
- Не выдумывай фактов, которых нет в state""",

    "en": """You are a warm, attentive copywriter. The input is JSON with structured business project state.

Write a "Your Clarity" document — in second person, as if telling a friend what they decided.

Structure:
1. **Where you are now** — current situation (from point_a)
2. **What you want** — desired outcome in the client's own words (from point_b)
3. **Why this matters** — motivation, pain, what changes in daily life
4. **What you decided** — key decisions from decision_log. Each as a simple statement: "You chose X because Y"
   If there are technical clarifications (marked [Technical clarification]) — add a 'Technical decisions' section with simple explanations.
5. **What's next** — next steps, timeline expectations

Rules:
- Second person: "You want...", "Your business...", "You decided..."
- BANNED: API, webhook, endpoint, SDK, LLM, MVP, backend, middleware, hosting, spec, widget
- Warm conversational tone, like explaining to a friend
- If a section has no data — skip silently
- Do not invent facts not in state""",
}

DEV_BRIEF_PROMPT: dict[str, str] = {
    "ru": """Ты — бизнес-аналитик. На вход — JSON со структурированным состоянием проекта.

Напиши «Бриф для разработчика» — понятный документ без кода и технических терминов.

Структура:
1. **Обзор проекта** — 1 абзац, что строим
2. **Список функций** — буллетами, простым языком
3. **Пользовательские сценарии** — Как [роль] хочу [действие] чтобы [результат]
4. **Бюджет и сроки** — ограничения из resources
5. **Критерии успеха** — измеримые результаты из point_b.success_metric
6. **Что НЕ входит** — границы из point_b.out_of_scope

Правила:
- Без кода, без архитектуры, без технических названий
- ЗАПРЕЩЕНО: API, webhook, endpoint, SDK, ORM, backend, middleware, cron, schema
- Названия сервисов (Telegram, ChatGPT) — можно
- Не выдумывай фактов""",

    "en": """You are a business analyst. The input is JSON with structured project state.

Write a "Developer Brief" — a clear document without code or technical terms.

Structure:
1. **Project Overview** — 1 paragraph, what we're building
2. **Features List** — bulleted, plain language
3. **User Stories** — As a [role] I want [action] so that [benefit]
4. **Budget & Timeline** — constraints from resources
5. **Success Criteria** — measurable outcomes from point_b.success_metric
6. **What's NOT Included** — boundaries from point_b.out_of_scope

Rules:
- No code, no architecture decisions, no technology names
- BANNED: API, webhook, endpoint, SDK, ORM, backend, middleware, cron, schema
- Service names (Telegram, ChatGPT) are fine
- Do not invent facts not in state""",
}


class ClarityWorkflow(Workflow):
    """V4 Clarity Workflow — guides from zero clarity to three structured outputs."""

    name = "v4"

    def __init__(self, llm: LLM):
        super().__init__(llm)

    # ---- public API ----

    def start(self, session: WorkflowSession) -> str:
        if session.state.conversation.phase == Phase.SCREENING:
            return session.transcript[-1]["content"] if session.transcript else ""
        lang = session.state.conversation.language
        opening = _OPENER[lang]
        session.append("assistant", opening, meta={
            "phase": "point_a",
            "kind": "opener",
            "internal_rationale": "v4 clarity greeting",
            "suggested_options": _OPENER_OPTIONS[lang],
        })
        session.state.conversation.phase = Phase.POINT_A
        return opening

    def respond(self, session: WorkflowSession, user_message: str) -> str:
        session.append("user", user_message)
        session.state.conversation.turn_count += 1

        # SPEC_REVIEW: PM handler
        if session.state.conversation.phase == Phase.SPEC_REVIEW:
            return self._handle_spec_review_turn(session, user_message)

        # ANALYSIS: solution shape selection
        if (
            session.state.conversation.phase == Phase.ANALYSIS
            and session.state.solution.shape is None
        ):
            return self._handle_analysis_turn(session, user_message)

        # DEV_COVERAGE: record answer
        if session.state.conversation.phase == Phase.DEV_COVERAGE:
            self._record_dev_coverage_answer(session, user_message)

        # 1. Extract + decision log
        self._extract(session)
        retracted_items = self._extract_session_memory(session, user_message)

        # 1b. Question-first responder
        question_answer: str | None = None
        if session.state.conversation.phase not in (Phase.DEV_COVERAGE, Phase.SPEC_REVIEW):
            question_answer = self._handle_user_question(session, user_message)

        # 2. Phase routing
        prev_phase = session.state.conversation.phase
        new_phase = session.state.next_phase()
        session.state.conversation.phase = new_phase
        phase_changed = new_phase != prev_phase

        # 3. DONE transition
        if new_phase == Phase.DONE and prev_phase != Phase.DONE:
            return self._emit_specs(session)

        # 3b. ANALYSIS entry
        if new_phase == Phase.ANALYSIS and prev_phase != Phase.ANALYSIS:
            return self._handle_analysis_turn(session, user_message)

        # 4. DEV_COVERAGE
        if new_phase == Phase.DEV_COVERAGE:
            return self._handle_dev_coverage_turn(session)

        # 5. Clarity checkpoint on phase change or every N turns
        lang = session.state.conversation.language
        turns_since_summary = (
            session.state.conversation.turn_count - session.state.conversation.last_summary_turn
        )
        should_checkpoint = phase_changed or (
            turns_since_summary >= CHECKPOINT_EVERY
            and not session.state.conversation.confirmed_summary
        )
        if should_checkpoint and any(self._has_any_facts(session.state)):
            text, meta = self._checkpoint_text(session, new_phase)
            text = self._prepend_answer(text, question_answer, lang=lang)
            text = self._postprocess_reply(session, text, skip_localize=True)
            session.append("assistant", text, meta=meta)
            session.state.conversation.last_summary_turn = session.state.conversation.turn_count
            return text

        # 6. Ask next question (with IDK handling baked into the prompt)
        idk_detected = self._detect_idk_signal(user_message, lang)
        text, meta = self._ask_next(session, idk_boost=idk_detected)
        text = self._prepend_answer(text, question_answer, lang=lang)
        text = self._prepend_retraction_ack(text, retracted_items, lang=lang)
        text = self._postprocess_reply(session, text)
        session.append("assistant", text, meta=meta)
        return text

    # ---- IDK detection ----

    def _detect_idk_signal(self, user_message: str, lang: str = "ru") -> bool:
        """Detect if user expressed uncertainty in their message."""
        msg_lower = user_message.lower().strip()
        signals = _IDK_SIGNALS_RU if lang == "ru" else _IDK_SIGNALS_EN
        return any(signal in msg_lower for signal in signals)

    @staticmethod
    def _prepend_retraction_ack(
        text: str, retracted_items: list[str] | None, lang: str = "ru",
    ) -> str:
        """Prepend brief ack for retracted items to the response."""
        if not retracted_items:
            return text
        if lang == "ru":
            acks = [f"Понял, убрал «{item}» из списка." for item in retracted_items]
        else:
            acks = [f"Got it — removed '{item}'." for item in retracted_items]
        prefix = " ".join(acks)
        return f"{prefix} {text}" if text else prefix

    # ---- extraction ----

    def _extract(self, session: WorkflowSession) -> None:
        """Extract structured data + log decisions."""
        recent = session.transcript[-6:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        lang = session.state.conversation.language
        resp = self.llm.complete(
            system=_extractor_system(lang),
            messages=messages,
            tools=[EXTRACTOR_TOOL, LOG_DECISION_TOOL],
            max_tokens=8000,
            temperature=0.0,
        )
        if resp.tool_use:
            name = resp.tool_use.get("name")
            input_data = resp.tool_use.get("input") or {}
            if name == "update_state":
                _merge_patch(session.state, input_data)
            elif name == "log_decision":
                self._record_decision(session, input_data)

        # Handle multi-tool responses from raw if available
        raw = resp.raw
        if raw and hasattr(raw, "content"):
            for block in raw.content:
                if getattr(block, "type", None) == "tool_use":
                    if block.name == "log_decision":
                        self._record_decision(session, block.input or {})
                    elif block.name == "update_state" and resp.tool_use.get("name") != "update_state":
                        _merge_patch(session.state, block.input or {})

        # Update clarity scores
        sol = session.state.solution
        sol.clarity_score = {
            "point_a": int(session.state.coverage_a() * 100),
            "point_b": int(session.state.coverage_b() * 100),
            "resources": int(session.state.coverage_resources() * 100),
        }

    def _record_decision(self, session: WorkflowSession, data: dict) -> None:
        """Append a decision log entry."""
        question = (data.get("question") or "").strip()
        answer = (data.get("answer") or "").strip()
        if not question or not answer:
            return
        entry = {
            "question": question,
            "answer": answer,
            "alternatives": data.get("alternatives") or [],
            "rationale": (data.get("rationale") or "").strip(),
        }
        session.state.solution.decision_log.append(entry)

    # ---- suggested options generation ----

    def _generate_suggested_options(self, session: WorkflowSession, question_text: str) -> list[str]:
        """Generate 2-4 suggested answer options for the given question.

        On any failure, returns an empty list so the flow is never broken.
        """
        lang = session.state.conversation.language
        try:
            state_summary = json.dumps(session.state.model_dump(mode="json"), ensure_ascii=False)
            user_msg = (
                f"Question: {question_text}\n\n"
                f"Business context:\n{state_summary}"
            )
            resp = self.llm.complete(
                system=_VARIANT_GEN_PROMPT[lang],
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=500,
                temperature=0.6,
            )
            verdict = _parse_json_verdict(resp.text or "")
            if not verdict:
                return []
            options = verdict.get("options") or []
            if not isinstance(options, list):
                return []
            # Validate: only strings, 2-4 items
            valid = [str(o).strip() for o in options if isinstance(o, str) and str(o).strip()]
            return valid[:4]
        except Exception:
            return []

    # ---- asking ----

    def _ask_next(self, session: WorkflowSession, *, idk_boost: bool = False) -> tuple[str, dict]:
        lang = session.state.conversation.language
        phase = session.state.conversation.phase
        next_field = spec_manifest.next_owner_field(session.state, phase)
        remaining = spec_manifest.owner_gaps(session.state, phase)

        system_prompt = _asker_system(session.state, lang=lang)
        if idk_boost:
            if lang == "ru":
                system_prompt += (
                    "\n\nПользователь выразил неуверенность. ОБЯЗАТЕЛЬНО: предложи 2-3 "
                    "конкретных примера из похожих бизнесов, дай аналогию, и предложи "
                    "варианты ответа (множественный выбор). НЕ переспрашивай тот же вопрос."
                )
            else:
                system_prompt += (
                    "\n\nThe user expressed uncertainty. YOU MUST: offer 2-3 concrete "
                    "examples from similar businesses, provide an analogy, and offer "
                    "multiple-choice options. Do NOT simply re-ask the same question."
                )

        recent = session.transcript[-8:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        resp = self.llm.complete(
            system=system_prompt,
            messages=messages,
            max_tokens=4000,
            temperature=0.6,
        )
        _fallback = {"ru": "Расскажите подробнее, пожалуйста.", "en": "Tell me more, please."}
        text = sanitize(resp.text or _fallback.get(lang, _fallback["ru"]), lang=lang)
        suggested = self._generate_suggested_options(session, text)
        meta = {
            "phase": phase.value,
            "target_field": next_field.name if next_field else None,
            "target_section": next_field.section if next_field else None,
            "remaining_gaps": [f.name for f in remaining],
            "kind": "asker",
            "internal_rationale": "v4 clarity asker with IDK handling",
            "suggested_options": suggested,
        }
        return text, meta

    # ---- checkpoint ----

    def _checkpoint_text(self, session: WorkflowSession, phase: Phase) -> tuple[str, dict]:
        apply_no_ai_if_rejected(session.state, session.transcript)
        filter_channels_to_user_history(session.state, session.transcript)
        recover_budget_from_history(session.state, session.transcript)
        lang = session.state.conversation.language
        text = sanitize(SUMMARY_TEMPLATE.render(state=session.state, phase=phase, lang=lang), lang=lang)
        meta = {
            "phase": phase.value,
            "kind": "clarity_checkpoint",
            "internal_rationale": "v4 clarity checkpoint — paraphrase + confirm",
            "suggested_options": _CHECKPOINT_OPTIONS[lang],
        }
        return text, meta

    # ---- postprocess ----

    def _postprocess_reply(
        self, session: WorkflowSession, text: str, *, skip_localize: bool = False,
    ) -> str:
        if not text:
            return text
        lang = session.state.conversation.language
        localized = (
            text if skip_localize else localize_foreign_fragments(text, lang, self.llm.complete)
        )
        memory_violations = check_against_memory(
            localized, session.state.conversation.session_memory,
        )
        if memory_violations:
            session.rule_violations.extend(f"session_memory: {v}" for v in memory_violations)
        return localized

    def _prepend_answer(self, text: str, answer: str | None, lang: str = "ru") -> str:
        if not answer:
            return text
        cleaned = sanitize(answer.strip(), lang=lang)
        return f"{cleaned}\n\n{text}" if cleaned else text

    # ---- question-first ----

    def _handle_user_question(self, session: WorkflowSession, user_message: str) -> str | None:
        if not user_message:
            return None
        msg_lower = user_message.lower().strip()
        cheap_signal = (
            "?" in msg_lower
            or msg_lower.startswith(("а ", "и ", "что ", "как ", "когда ", "почему ", "сколько ", "можно ", "это "))
            or " ли " in msg_lower
            or msg_lower.startswith(("what ", "how ", "why ", "when ", "can ", "is ", "do "))
        )
        if not cheap_signal:
            return None

        lang = session.state.conversation.language
        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [
                {"role": m["role"], "content": m["content"]}
                for m in session.transcript[-10:]
            ],
            "user_message": user_message,
        }
        try:
            resp = self.llm.complete(
                system=ANSWER_USER_QUESTION_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=2000,
                temperature=0.2,
            )
            verdict = _parse_json_verdict(resp.text or "")
        except Exception:
            return None
        if not verdict or not verdict.get("has_question"):
            return None
        return (verdict.get("answer") or "").strip() or None

    # ---- session memory ----

    def _extract_session_memory(self, session: WorkflowSession, user_message: str) -> list[str] | None:
        """Extract session memory (retractions/preferences). Returns list of retracted item values."""
        if not user_message or len(user_message.strip()) < 2:
            return None
        msg_lower = user_message.lower()
        signal_terms = (
            "не ", "нет", "убер", "удал", "без ", "не нужн", "не хочу", "не мой",
            "не пользу", "только ", "лень", "не дороже", "максимум", "минимум",
            "no ", "not ", "don't", "without ", "skip", "remove", "drop",
        )
        if not any(term in msg_lower for term in signal_terms):
            return None

        payload = {
            "user_message": user_message,
            "state_summary": {
                "channels": session.state.point_a.channels,
                "tools_in_use": session.state.point_a.tools_in_use,
                "has_llm_key": session.state.resources.has_llm_key,
                "monthly_budget_rub": session.state.resources.monthly_budget_rub,
            },
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=EXTRACT_MEMORY_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=1500,
                temperature=0.0,
            )
            verdict = _parse_json_verdict(resp.text or "") or {}
        except Exception:
            return None

        memory = session.state.conversation.session_memory
        turn = session.state.conversation.turn_count

        retracted_items: list[str] = []
        for entry in verdict.get("retractions") or []:
            if not isinstance(entry, dict):
                continue
            value = (entry.get("value") or "").strip()
            if not value:
                continue
            memory.retractions.append({
                "type": (entry.get("type") or "feature").strip(),
                "value": value,
                "turn": turn,
                "user_quote": (entry.get("user_quote") or "").strip()[:200],
            })
            self._reconcile_retraction(session, entry)
            retracted_items.append(value)

        for entry in verdict.get("preferences") or []:
            if not isinstance(entry, dict):
                continue
            topic = (entry.get("topic") or "").strip()
            if not topic:
                continue
            memory.preferences.append({
                "topic": topic,
                "value": (entry.get("value") or "").strip(),
                "turn": turn,
                "user_quote": (entry.get("user_quote") or "").strip()[:200],
            })
            self._reconcile_preference(session, entry)

        return retracted_items or None

    @staticmethod
    def _reconcile_retraction(session: WorkflowSession, entry: dict) -> None:
        from .postprocess import canonicalize as _canon
        rtype = (entry.get("type") or "").lower()
        value = (entry.get("value") or "").strip()
        if not value:
            return
        canon_value = _canon(value).lower()
        target_lists: list[list[str]] = []
        if rtype == "channel":
            target_lists.append(session.state.point_a.channels)
        elif rtype == "tool":
            target_lists.append(session.state.point_a.tools_in_use)
            target_lists.append(session.state.resources.integrations_available)
            target_lists.append(session.state.resources.integrations_needed)
        elif rtype == "feature":
            target_lists.append(session.state.point_b.functional_requirements_owner)
            target_lists.append(session.state.point_b.user_flows)
        elif rtype == "resource":
            if canon_value in {"ии", "llm", "chatgpt", "claude", "gemini", "ai"}:
                session.state.resources.has_llm_key = None
                session.state.resources.llm_provider = None
                session.state.resources.llm_required = False
            return
        for lst in target_lists:
            if not lst:
                continue
            kept = [item for item in lst if _canon(item).lower() != canon_value]
            lst.clear()
            lst.extend(kept)

    @staticmethod
    def _reconcile_preference(session: WorkflowSession, entry: dict) -> None:
        topic = (entry.get("topic") or "").lower()
        if topic == "no_ai":
            session.state.resources.llm_required = False
            session.state.resources.has_llm_key = None
            session.state.resources.llm_provider = None

    # ---- ANALYSIS phase (reuses v2 pattern) ----

    ANALYSIS_GAP_FILL_CAP = 3

    def _handle_analysis_turn(self, session: WorkflowSession, user_message: str) -> str:
        sol = session.state.solution
        if sol.offered_options or sol.analysis_qa or self._has_recent_gap_question(session):
            verdict = self._analysis_pm_classify(session, user_message)
            intent = (verdict.get("intent") or "unclear").lower()
            if intent == "pick":
                idx = verdict.get("chosen_index")
                if isinstance(idx, int) and 1 <= idx <= len(sol.offered_options):
                    return self._lock_solution_and_ack(session, idx - 1)
                return self._reemit_options(session)
            if intent == "more_options":
                sol.offered_options = []
                return self._generate_and_emit_options(session, more=True)
            if intent == "correct":
                idx = verdict.get("chosen_index")
                correction = (verdict.get("correction") or "").strip()
                if isinstance(idx, int) and 1 <= idx <= len(sol.offered_options) and correction:
                    return self._lock_solution_with_correction(session, idx - 1, correction)
                return self._reemit_options(session)
            if intent == "question":
                lang = session.state.conversation.language
                answer = sanitize((verdict.get("answer") or "").strip(), lang=lang) or "Let me clarify."
                reprompt = {"ru": "Подходит вариант или нужны другие?", "en": "Does one work, or need others?"}
                text = self._postprocess_reply(session, f"{answer}\n\n{reprompt.get(lang, reprompt['en'])}")
                session.append("assistant", text, meta={"phase": Phase.ANALYSIS.value, "kind": "analysis_answer"})
                return text
            if intent == "gap_answer":
                gap_answer = (verdict.get("gap_answer") or user_message).strip()
                last_q = self._last_gap_question(session)
                if last_q:
                    sol.analysis_qa.append({"question": last_q, "answer": gap_answer, "gap": ""})
                return self._scanner_or_options(session)
            # unclear
            if sol.offered_options:
                lang = session.state.conversation.language
                prefix = {"ru": "Не понял. Выберите вариант или скажите что не так.", "en": "Didn't catch that. Pick an option or tell me what's off."}
                return self._reemit_options(session, prefix=prefix.get(lang, prefix["en"]))
            return self._scanner_or_options(session)
        return self._scanner_or_options(session)

    def _scanner_or_options(self, session: WorkflowSession) -> str:
        sol = session.state.solution
        iteration = len(sol.analysis_qa) + 1
        if iteration > self.ANALYSIS_GAP_FILL_CAP:
            return self._generate_and_emit_options(session)
        verdict = self._analysis_run_scanner(session, iteration)
        action = (verdict.get("action") or "propose_options").lower()
        if action == "ask_gap" and iteration <= self.ANALYSIS_GAP_FILL_CAP:
            lang = session.state.conversation.language
            question = sanitize((verdict.get("gap_question") or "").strip(), lang=lang)
            if not question:
                return self._generate_and_emit_options(session)
            text = self._postprocess_reply(session, question)
            session.append("assistant", text, meta={
                "phase": Phase.ANALYSIS.value,
                "kind": "analysis_gap_question",
                "iteration": iteration,
            })
            return text
        return self._generate_and_emit_options(session)

    def _analysis_run_scanner(self, session: WorkflowSession, iteration: int) -> dict:
        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [{"role": m["role"], "content": m["content"]} for m in session.transcript[-10:]],
            "iteration": iteration,
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=ANALYSIS_SCANNER_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=2000, temperature=0.2,
            )
            return _parse_json_verdict(resp.text or "") or {"action": "propose_options"}
        except Exception:
            return {"action": "propose_options"}

    def _generate_and_emit_options(self, session: WorkflowSession, *, more: bool = False) -> str:
        catalog_json = get_automation_catalog()
        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [{"role": m["role"], "content": m["content"]} for m in session.transcript[-10:]],
            "automation_catalog": json.loads(catalog_json) if catalog_json != "{}" else None,
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=ANALYSIS_OPTIONS_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=6000, temperature=0.4,
            )
            verdict = _parse_json_verdict(resp.text or "") or {}
        except Exception:
            verdict = {}
        options = verdict.get("options") or []
        if not options:
            fallback = {"ru": "Расскажите ещё — пока маловато для вариантов.", "en": "Tell me more — I need a bit more info to suggest options."}
            text = self._postprocess_reply(session, fallback.get(lang, fallback["en"]))
            session.append("assistant", text, meta={"phase": Phase.ANALYSIS.value, "kind": "analysis_options_fallback"})
            return text

        # Validate recommended: ensure exactly one option is marked recommended
        if options:
            has_recommended = any(opt.get("recommended") for opt in options)
            if not has_recommended:
                # Mark first option as recommended by default
                options[0]["recommended"] = True
            else:
                # Ensure only ONE is marked recommended (keep first found)
                found_first = False
                for opt in options:
                    if opt.get("recommended"):
                        if found_first:
                            opt["recommended"] = False
                        else:
                            found_first = True

        session.state.solution.offered_options = options
        message = self._render_options_message(options, verdict.get("intro") or "", more=more, lang=lang)
        text = self._postprocess_reply(session, message, skip_localize=True)
        session.append("assistant", text, meta={"phase": Phase.ANALYSIS.value, "kind": "analysis_options"})
        return text

    def _render_options_message(self, options: list[dict], intro: str, *, more: bool = False, lang: str = "en") -> str:
        s = {
            "ru": {"header": "Вариант", "pick": "Скажите номер (1, 2 или 3) или напишите правки/вопросы."},
            "en": {"header": "Option", "pick": "Say the number (1, 2 or 3), or write edits/questions."},
        }.get(lang, {"header": "Option", "pick": "Say the number (1, 2 or 3)."})
        lines: list[str] = [intro or ("Here are a few paths:" if lang == "en" else "Вот пара путей:"), ""]
        for i, opt in enumerate(options, 1):
            shape = (opt.get("shape") or f"{s['header']} {i}").strip()
            desc = (opt.get("description") or "").strip()
            rec = " ⭐" if opt.get("recommended") else ""
            lines.append(f"**{s['header']} {i} — {shape}{rec}**")
            if desc:
                lines.append(desc)
            lines.append("")
        lines.append(s["pick"])
        return "\n".join(lines).rstrip()

    def _analysis_pm_classify(self, session: WorkflowSession, user_message: str) -> dict:
        payload = {
            "offered_options": session.state.solution.offered_options,
            "transcript_tail": [{"role": m["role"], "content": m["content"]} for m in session.transcript[-10:]],
            "user_message": user_message,
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=ANALYSIS_PM_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=2000, temperature=0.2,
            )
            return _parse_json_verdict(resp.text or "") or {"intent": "unclear"}
        except Exception:
            return {"intent": "unclear"}

    def _lock_solution_and_ack(self, session: WorkflowSession, opt_index: int) -> str:
        chosen = session.state.solution.offered_options[opt_index]
        sol = session.state.solution
        sol.shape = (chosen.get("shape") or "").strip() or f"option {opt_index + 1}"
        sol.shape_description = (chosen.get("description") or "").strip()
        sol.needs_ai = bool(chosen.get("needs_ai"))
        sol.needs_chat_ui = bool(chosen.get("needs_chat_ui"))
        sol.needs_external_integrations = bool(chosen.get("needs_external_integrations"))
        session.state.resources.llm_required = sol.needs_ai
        if sol.needs_ai is False:
            session.state.resources.has_llm_key = None
            session.state.resources.llm_provider = None
            session.state.resources.llm_subscription_quote = None

        # Log this as a decision
        self._record_decision(session, {
            "question": "Which solution shape to build?",
            "answer": sol.shape,
            "alternatives": [
                o.get("shape", "") for i, o in enumerate(sol.offered_options) if i != opt_index
            ],
            "rationale": sol.shape_description or "Owner's choice",
        })

        lang = session.state.conversation.language
        ack = {
            "ru": f"Окей, идём с: {sol.shape}.\n\nТеперь ещё немного про ресурсы.",
            "en": f"Got it — going with: {sol.shape}.\n\nNow a few more questions about resources.",
        }
        text = self._postprocess_reply(session, ack.get(lang, ack["en"]))
        session.append("assistant", text, meta={"phase": Phase.ANALYSIS.value, "kind": "analysis_lock", "shape": sol.shape})
        return text

    def _lock_solution_with_correction(self, session: WorkflowSession, opt_index: int, correction: str) -> str:
        chosen = session.state.solution.offered_options[opt_index]
        chosen_with_fix = dict(chosen)
        original_desc = (chosen.get("description") or "").strip()
        chosen_with_fix["description"] = f"{original_desc} (with owner correction: {correction})" if original_desc else f"With correction: {correction}"
        session.state.solution.offered_options[opt_index] = chosen_with_fix
        return self._lock_solution_and_ack(session, opt_index)

    def _reemit_options(self, session: WorkflowSession, *, prefix: str = "") -> str:
        options = session.state.solution.offered_options or []
        if not options:
            return self._scanner_or_options(session)
        lang = session.state.conversation.language
        message = self._render_options_message(options, intro=prefix, lang=lang)
        text = self._postprocess_reply(session, message, skip_localize=True)
        session.append("assistant", text, meta={"phase": Phase.ANALYSIS.value, "kind": "analysis_options_reemit"})
        return text

    def _has_recent_gap_question(self, session: WorkflowSession) -> bool:
        for m in reversed(session.transcript[:-1]):
            if m.get("role") == "assistant":
                return (m.get("meta") or {}).get("kind") == "analysis_gap_question"
            if m.get("role") == "user":
                continue
        return False

    def _last_gap_question(self, session: WorkflowSession) -> str | None:
        for m in reversed(session.transcript[:-1]):
            if m.get("role") == "assistant":
                meta = m.get("meta") or {}
                if meta.get("kind") == "analysis_gap_question":
                    return m.get("content")
                return None
        return None

    # ---- DEV_COVERAGE ----

    def _handle_dev_coverage_turn(self, session: WorkflowSession) -> str:
        qa_list = session.state.resources.dev_coverage_qa
        iteration = len(qa_list) + 1
        if iteration > DEV_COVERAGE_MAX_ITERATIONS:
            return self._proceed_after_dev_coverage_complete(session)

        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [{"role": m["role"], "content": m["content"]} for m in session.transcript[-20:]],
            "iteration": iteration,
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=SPEC_COVERAGE_SCAN_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=4000, temperature=0.3,
            )
            verdict = _parse_json_verdict(resp.text or "") or {"complete": True}
        except Exception:
            verdict = {"complete": True}

        if verdict.get("complete") is True:
            return self._proceed_after_dev_coverage_complete(session)

        fallback = {"ru": "Уточните, пожалуйста.", "en": "Could you clarify?"}
        question = sanitize((verdict.get("question") or "").strip() or fallback.get(lang, fallback["en"]), lang=lang)
        question = self._postprocess_reply(session, question)
        session.append("assistant", question, meta={
            "phase": Phase.DEV_COVERAGE.value,
            "kind": "dev_coverage_question",
            "iteration": iteration,
        })
        return question

    def _record_dev_coverage_answer(self, session: WorkflowSession, user_message: str) -> None:
        existing_qa = session.state.resources.dev_coverage_qa
        existing_questions = {entry.get("question") for entry in existing_qa}
        last_q: str | None = None
        last_rationale = ""
        for m in reversed(session.transcript[:-1]):
            meta = m.get("meta") or {}
            if m.get("role") == "assistant" and meta.get("kind") == "dev_coverage_question":
                q = m.get("content") or ""
                if q in existing_questions:
                    return
                last_q = q
                last_rationale = (meta.get("rationale") or "")[:200]
                break
            if m.get("role") == "user":
                continue
        if not last_q:
            return
        session.state.resources.dev_coverage_qa.append({
            "question": last_q,
            "answer": user_message,
            "rationale": last_rationale,
        })

    def _proceed_after_dev_coverage_complete(self, session: WorkflowSession) -> str:
        session.state.conversation.dev_coverage_complete = True

        # Merge dev_coverage_qa into decision_log as architectural decisions
        for qa_entry in session.state.resources.dev_coverage_qa:
            self._record_decision(session, {
                "question": qa_entry.get("question", ""),
                "answer": qa_entry.get("answer", ""),
                "alternatives": [],
                "rationale": f"[Technical clarification] {qa_entry.get('rationale', '')}".strip(),
            })

        new_phase = session.state.next_phase()
        session.state.conversation.phase = new_phase
        text, meta = self._checkpoint_text(session, new_phase)
        text = self._postprocess_reply(session, text, skip_localize=True)
        session.append("assistant", text, meta=meta)
        session.state.conversation.last_summary_turn = session.state.conversation.turn_count
        return text

    # ---- SPEC_REVIEW ----

    def _handle_spec_review_turn(self, session: WorkflowSession, user_message: str) -> str:
        normalized = (user_message or "").strip().rstrip(".!").lower()
        if normalized == CONFIRM_BUTTON_TEXT.lower():
            session.state.conversation.confirmed_summary = True
            return self._emit_specs(session)

        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [{"role": m["role"], "content": m["content"]} for m in session.transcript[-12:]],
            "user_message": user_message,
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=PM_REVIEW_PROMPT[lang],
                messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)}],
                max_tokens=2500, temperature=0.2,
            )
            verdict = _parse_json_verdict(resp.text or "") or {"intent": "question", "answer": "Could you rephrase?"}
        except Exception:
            verdict = {"intent": "question", "answer": "Sorry, could you rephrase?"}

        intent = (verdict.get("intent") or "question").lower()

        if intent == "confirm":
            session.state.conversation.confirmed_summary = True
            return self._emit_specs(session)

        if intent == "correct":
            patch = verdict.get("patch") or {}
            if patch:
                _apply_pm_patch(session.state, patch)
            ack = sanitize((verdict.get("ack") or "Fixed.").strip(), lang=lang)
            summary, _ = self._checkpoint_text(session, Phase.SPEC_REVIEW)
            text = self._postprocess_reply(session, f"{ack}\n\n{summary}", skip_localize=True)
            session.append("assistant", text, meta={"phase": Phase.SPEC_REVIEW.value, "kind": "pm_correction_ack"})
            session.state.conversation.last_summary_turn = session.state.conversation.turn_count
            return text

        # question / unknown
        reprompt = {"ru": "Если вопросов больше нет — нажмите «да, вопросов нет».", "en": "If no more questions — tap «yes, no questions»."}
        answer = sanitize((verdict.get("answer") or "Good question.").strip(), lang=lang)
        text = self._postprocess_reply(session, f"{answer}\n\n{reprompt.get(lang, reprompt['en'])}")
        session.append("assistant", text, meta={"phase": Phase.SPEC_REVIEW.value, "kind": "pm_answer"})
        return text

    # ---- Three-document spec emission ----

    _ESTIMATE_PROMPTS: dict[str, str] = {
        "ru": "Дай грубую вилку срока реализации одной короткой фразой. Примеры: '2-3 недели', 'около недели'.",
        "en": "Give a rough delivery-time range as one short phrase. E.g. '2-3 weeks', 'about a week'.",
    }

    def _estimate_duration(self, state_json: str, lang: str = "en") -> str:
        fallback = {"ru": "уточним по ходу", "en": "we'll clarify as we go"}
        try:
            resp = self.llm.complete(
                system=self._ESTIMATE_PROMPTS.get(lang, self._ESTIMATE_PROMPTS["en"]),
                messages=[{"role": "user", "content": state_json}],
                max_tokens=500, temperature=0.2,
            )
            txt = (resp.text or "").strip().strip(".").strip("«»\"'")
            return txt or fallback.get(lang, fallback["en"])
        except Exception:
            return fallback.get(lang, fallback["en"])

    def _emit_specs(self, session: WorkflowSession) -> str:
        """Generate THREE output documents and emit final ack."""
        lang = session.state.conversation.language
        state_dump = session.state.model_dump(mode="json")
        state_json = json.dumps(state_dump, ensure_ascii=False, indent=2)
        transcript_tail = session.transcript[-20:]
        skill_content = load_skill("triple-spec-writer")

        # Output A — Clarity Document
        clarity_raw = self.llm.complete(
            system=CLARITY_DOC_PROMPT[lang] + "\n\n---\n\n" + skill_content,
            messages=[{"role": "user", "content": state_json}],
            max_tokens=12000,
            temperature=0.4,
        ).text or ""
        clarity_doc = sanitize(clarity_raw, lang=lang)

        # Output B — Developer Brief
        brief_raw = self.llm.complete(
            system=DEV_BRIEF_PROMPT[lang] + "\n\n---\n\n" + skill_content,
            messages=[{"role": "user", "content": state_json}],
            max_tokens=12000,
            temperature=0.3,
        ).text or ""
        biz_spec = sanitize(brief_raw, lang=lang)

        # Output C — Technical Specification
        dev_payload = {
            "state": state_dump,
            "transcript_tail": transcript_tail,
        }
        dev_system = DEV_SPEC_PROMPT[lang] + "\n\n---\n\n# Skill: triple-spec-writer\n\n" + skill_content
        dev_raw = self.llm.complete(
            system=dev_system,
            messages=[{"role": "user", "content": json.dumps(dev_payload, ensure_ascii=False, indent=2)}],
            max_tokens=20000,
            temperature=0.2,
        ).text or ""
        dev_spec = strip_dev_mentions(dev_raw)

        violations = check_variant_blocks(dev_spec, lang=lang)
        if violations:
            session.rule_violations.extend(violations)

        # Store all three
        session.clarity_doc = clarity_doc
        session.business_spec = biz_spec
        session.dev_spec = dev_spec
        session.state.conversation.phase = Phase.DONE
        if session.state.conversation.ended_at is None:
            session.state.conversation.ended_at = datetime.now(timezone.utc)

        # Final ack
        estimate = self._estimate_duration(state_json, lang=lang)
        session.state.conversation.estimate_text = estimate
        ack = self._postprocess_reply(session, FINAL_ACK_TEMPLATE[lang].format(estimate=estimate))
        session.append("assistant", ack, meta={
            "phase": "done",
            "kind": "spec_emission",
            "internal_rationale": (
                "v4 clarity: three specs saved (clarity + brief + dev), "
                "owner gets ack + estimate"
            ),
        })
        return ack

    @staticmethod
    def _has_any_facts(state: WorkflowState) -> tuple[bool, ...]:
        return (
            state.coverage_a() > 0,
            state.coverage_b() > 0,
            state.coverage_resources() > 0,
        )


# ---- Parsing helpers ----

def _apply_pm_patch(state: WorkflowState, patch: dict) -> None:
    """Apply a PM-issued correction patch (replaces lists, not appends)."""
    for section_key in ("point_a", "point_b", "resources"):
        section_patch = patch.get(section_key) or {}
        section = getattr(state, section_key, None)
        if section is None:
            continue
        for k, v in section_patch.items():
            if not hasattr(section, k):
                continue
            existing = getattr(section, k)
            if isinstance(existing, list):
                setattr(section, k, list(v) if isinstance(v, list) else [])
            else:
                setattr(section, k, v)
    if patch.get("user_confirmed_summary"):
        state.conversation.confirmed_summary = True


def _parse_json_verdict(text: str) -> dict | None:
    """Parse JSON from LLM response, tolerating code fences and stray text."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(raw[start:end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
