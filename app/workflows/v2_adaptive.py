"""V2 — Adaptive State Machine.

Architecture per turn:
  1. Extractor LLM call with tool use → updates structured state (PointA / PointB / Resources)
  2. Deterministic phase router → advances phase when coverage threshold reached
  3. Either a deterministic checkpoint summary, or
     an Asker LLM call that targets the top open gaps for the current phase

When the state is complete, two final LLM calls produce the business spec and the
dev spec, both grounded in the structured state (not in chat history).
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
    CONVERSATIONAL_STYLE,
    DETECT_LLM_REQUIREMENT_PROMPT,
    DEV_SPEC_PROMPT,
    EXTRACT_MEMORY_PROMPT,
    FINAL_ACK_TEMPLATE,
    PM_REVIEW_PROMPT,
    SPEC_COVERAGE_SCAN_PROMPT,
    SUMMARY_TEMPLATE,
)
from .postprocess import (
    apply_no_ai_if_rejected,
    canonicalize_list,
    check_against_memory,
    check_variant_blocks,
    filter_channels_to_user_history,
    localize_english_fragments,
    localize_foreign_fragments,
    recover_budget_from_history,
    sanitize,
    strip_dev_mentions,
)

DEV_COVERAGE_MAX_ITERATIONS = 8


_OPENER: dict[str, str] = {
    "ru": (
        "Привет! Я помогу разобраться, что вашему бизнесу нужно автоматизировать или построить, "
        "и в конце соберу понятный план с конкретными шагами и расходами.\n\n"
        "С чего удобнее начать?\n"
        "— У меня магазин (соцсети / маркетплейс / сайт) и теряются заявки\n"
        "— Я оказываю услуги, хочу убрать рутину\n"
        "— Хочу телеграм-бота, но не знаю какого\n"
        "— Не знаю, чего хочу — давайте разбираться вместе"
    ),
    "en": (
        "Hi! I'll help figure out what your business should automate or build, "
        "and at the end I'll put together a clear plan with concrete steps and costs.\n\n"
        "Where would it be easiest to start?\n"
        "— I have a store (social media / marketplace / website) and orders slip through\n"
        "— I offer services and want to cut the routine\n"
        "— I want a Telegram bot but don't know which kind\n"
        "— I don't know what I want — let's figure it out together"
    ),
}

CHECKPOINT_EVERY = 5  # turns

# Exact text the front-end button «да, вопросов нет» sends. Recognised
# deterministically as confirmation; no LLM classification needed for this
# path. Free-form text goes through PM-LLM (see _handle_spec_review_turn).
CONFIRM_BUTTON_TEXT = "да, вопросов нет, подтверждаю"

EXTRACTOR_TOOL = {
    "name": "update_state",
    "description": (
        "Извлечь из последнего ответа пользователя факты о его бизнесе и обновить "
        "структурированное состояние. Передавай ТОЛЬКО реально новые/уточнённые поля "
        "из последнего сообщения. Не выдумывай, если факта не было — не передавай."
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
                            "Monthly willingness to pay for tools/subscriptions, plain integer. "
                            "CURRENCY-AGNOSTIC — the legacy '_rub' suffix in the name is for "
                            "Firestore back-compat only; the value carries NO currency unit. "
                            "STORE THE NUMBER AS-IS in the owner's own currency. NEVER apply an "
                            "exchange rate. NEVER multiply by 100 / 1000 / 90. "
                            "RU forms: «5000» → 5000, «5 тысяч» → 5000, «5к» → 5000, «до 1000» → 1000. "
                            "EN forms: '$5000' → 5000, '5K' → 5000, '$50/mo' → 50, 'around $30' → 30. "
                            "RANGES — store the UPPER bound: '$40-60/mo' → 60, '5-20 тысяч' → 20000, "
                            "'$10-50' → 50. "
                            "Only extract when the owner is actually talking about budget / "
                            "subscriptions / spending."
                        ),
                    },
                    "has_llm_key": {"type": "boolean"},
                    "llm_provider": {"type": "string"},
                    "llm_subscription_quote": {
                        "type": "string",
                        "description": (
                            "ДОСЛОВНАЯ фраза пользователя про наличие/отсутствие подписки "
                            "на ИИ-сервис (ChatGPT/Claude/Gemini/YandexGPT/GigaChat). "
                            "Сохраняй точно как сказал — не переформулируй, не сокращай, "
                            "не дополняй. Заполняй ТОЛЬКО когда пользователь сам ответил "
                            "на вопрос про подписку. Если темы не было в его сообщении — "
                            "это поле НЕ передавай."
                        ),
                    },
                    "integrations_available": {"type": "array", "items": {"type": "string"}},
                    "integrations_needed": {"type": "array", "items": {"type": "string"}},
                    "tech_savviness": {
                        "type": "string",
                        "enum": ["none", "basic", "confident"],
                    },
                    "maintainer": {
                        "type": "string",
                        "description": (
                            "Who maintains the system after launch — VERBATIM owner phrase "
                            "in their own language. E.g. 'I'll maintain it myself', "
                            "'мой ассистент Лена', 'we'll hand off to our IT guy', "
                            "'наш разработчик Игорь'. Do NOT canonicalize to a short token "
                            "like 'user' / 'owner' / 'team' / 'external'. Preserve exact wording."
                        ),
                    },
                    "deployment_constraints": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "preferred_channel": {"type": "string"},
                },
            },
            "user_confirmed_summary": {
                "type": "boolean",
                "description": "True если пользователь подтвердил последний пересказ ('да', 'верно', 'так')",
            },
        },
        "additionalProperties": False,
    },
}


_CANONICALIZED_LIST_FIELDS = {
    # Brand-name lists where ru/en transliteration variants must collapse.
    "channels", "tools_in_use", "integrations_available", "integrations_needed",
    "deployment_constraints",
}


def _merge_patch(state: WorkflowState, patch: dict) -> None:
    """Apply a partial update from the extractor tool to the state.

    List dedup is case- and whitespace-insensitive: «Word», «word», « Word »
    all collapse to a single entry. Without this, Gemini's extractor produced
    «word, excel, 1с, Word, Excel, 1C» in the tools list because each turn it
    re-extracted the same items in user-typed casing.

    For brand/tool lists (channels, tools_in_use, integrations_*) we go further
    and collapse ru/en transliteration variants («Notion»/«нотион»,
    «Тильда»/«Tilda», «ЮKassa»/«юкасса») via the CANONICAL_NAMES table. This
    and prevent repeated entities with different spellings from being stored.
    """

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
                # Re-canonicalize the merged list, collapsing ru/en variants.
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


def _extractor_system() -> str:
    return (
        "Ты — STRUCTURED-EXTRACTOR. Твоя задача — прочитать последний ответ пользователя "
        "в контексте всего диалога и вызвать инструмент `update_state` с теми полями, "
        "которые однозначно вытекают из этого ответа. Если ничего нового нет — всё равно "
        "вызови инструмент с пустыми патчами. НЕ пиши никакого текста, только tool call."
    )


def _asker_system(state: WorkflowState, lang: str = "ru") -> str:
    phase = state.conversation.phase
    next_field = spec_manifest.next_owner_field(state, phase)
    remaining = spec_manifest.owner_gaps(state, phase)
    llm_required = state.resources.llm_required

    if lang == "en":
        resources_focus = (
            "Now exploring resources — budget, access, support. If they have no AI subscription — "
            "after the discussion offer to provide instructions on how to get one."
            if llm_required is not False
            else (
                "Now exploring resources — budget, access, support. "
                "IMPORTANT: this case does NOT require an AI service — skip any questions about "
                "a ChatGPT / Claude / Gemini subscription or API keys. "
                "Also do NOT ask 'do you have an AI access' — the field is not needed. "
                "Focus on the tool/service budget (Make.com / Zapier / integrations) "
                "and who will maintain the system after launch."
            )
        )
        phase_focus = {
            Phase.POINT_A: (
                "Collecting Point A — how things work for the user today. "
                "Close gaps one question at a time. Offer 3–4 options."
            ),
            Phase.POINT_B: (
                "Exploring Point B — what should change. "
                "Most important: a measurable success metric and an explicit 'what we are NOT doing'."
            ),
            Phase.RESOURCES: resources_focus,
            Phase.SPEC_REVIEW: (
                "Final recap before the plan. Confirm the key facts "
                "and ask 'does this look right?' — after 'yes' you'll produce the plan."
            ),
        }.get(phase, "")

        if next_field is not None:
            if llm_required is False and next_field.name in ("has_llm_key", "llm_provider"):
                focus_hint = (
                    "\n\n## What to ask now\nThis case doesn't need AI — "
                    "move to the next resource item (budget / support / channels)."
                )
            else:
                focus_hint = (
                    f"\n\n## What to ask now\n"
                    f"The goal of this turn is field `{next_field.name}` (section '{next_field.section}').\n"
                    f"Suggested phrasing (feel free to rephrase for context, but preserve the meaning):\n"
                    f"'{next_field.owner_question[lang]}'"
                )
        else:
            focus_hint = "\n\n## What to ask now\nAll fields in this phase are closed — move to recap or confirmation."

        remaining_names = ", ".join(f.name for f in remaining) if remaining else "none — phase ready"
        one_q_instruction = "Ask ONE question to the user. No preamble. Offer options when appropriate."
    else:
        resources_focus = (
            "Сейчас выясняешь ресурсы — бюджет, доступы, поддержку. Если нет подписки на ИИ — "
            "после обсуждения предложи выдать инструкцию, как её оформить."
            if llm_required is not False
            else (
                "Сейчас выясняешь ресурсы — бюджет, доступы, поддержку. "
                "ВАЖНО: для этого кейса ИИ-сервис НЕ требуется — пропусти любые вопросы про "
                "подписку на ChatGPT / Claude / YandexGPT и про API-ключи. "
                "Также НЕ задавай вопрос «есть ли у вас доступ к ИИ» — это поле не нужно. "
                "Сосредоточься на бюджете на инструменты-сервисы (Make.com / Zapier / интеграции) "
                "и на том, кто будет поддерживать систему после запуска."
            )
        )
        phase_focus = {
            Phase.POINT_A: (
                "Сейчас собираешь Точку А — как у пользователя всё устроено сегодня. "
                "Закрывай пробелы по списку, по одному вопросу за раз. Предлагай 3–4 варианта."
            ),
            Phase.POINT_B: (
                "Сейчас выясняешь Точку Б — что должно стать иначе. "
                "Особенно важны: измеримая метрика успеха и явное 'что НЕ делаем'."
            ),
            Phase.RESOURCES: resources_focus,
            Phase.SPEC_REVIEW: (
                "Сейчас финальный пересказ перед планом. Подтверди ключевые факты "
                "и спроси 'всё так?' — после 'да' выдашь план."
            ),
        }.get(phase, "")

        if next_field is not None:
            if llm_required is False and next_field.name in ("has_llm_key", "llm_provider"):
                focus_hint = (
                    "\n\n## Что спросить сейчас\nДля этого кейса ИИ не нужен — "
                    "перейди к следующему ресурсному пункту (бюджет / поддержка / каналы)."
                )
            else:
                focus_hint = (
                    f"\n\n## Что спросить сейчас\n"
                    f"Цель этого хода — поле `{next_field.name}` (раздел «{next_field.section}»).\n"
                    f"Подсказка-формулировка (можно перефразировать под контекст диалога, "
                    f"но смысл сохрани):\n«{next_field.owner_question[lang]}»"
                )
        else:
            focus_hint = "\n\n## Что спросить сейчас\nВсе поля этой фазы закрыты — переходи к пересказу или подтверждению."

        remaining_names = ", ".join(f.name for f in remaining) if remaining else "нет — фаза готова"
        one_q_instruction = "Задай ОДИН вопрос пользователю. Без преамбул. Предлагай варианты, когда уместно."

    return (
        CONVERSATIONAL_STYLE[lang]
        + _memory_constraint_block(state, lang=lang)
        + "\n\n## Текущая фаза\n" + phase_focus
        + focus_hint
        + f"\n\n## Ещё не закрыто в этой фазе: {remaining_names}"
        + "\n\n## Текущее состояние (только для тебя, не цитируй буквально):\n"
        + json.dumps(state.model_dump(mode='json'), ensure_ascii=False, indent=2)
        + f"\n\n{one_q_instruction}"
    )


def _memory_constraint_block(state: WorkflowState, lang: str = "ru") -> str:
    """Render an inline constraint block for asker/scanner/PM/spec-writer
    prompts based on session_memory contents. Empty string when no memory."""
    memory = state.conversation.session_memory
    retractions = memory.retractions or []
    preferences = memory.preferences or []
    if not retractions and not preferences:
        return ""
    if lang == "en":
        lines = ["\n\n## What the owner has ALREADY EXCLUDED or RESTRICTED — do NOT bring these back:"]
        for r in retractions:
            lines.append(
                f"- REMOVED ({r.get('type', '?')}): '{r.get('value', '')}' — "
                f"'{r.get('user_quote', '')}'"
            )
        for p in preferences:
            lines.append(
                f"- PREFERENCE ({p.get('topic', '?')}): {p.get('value', '')} — "
                f"'{p.get('user_quote', '')}'"
            )
        lines.append(
            "If you were planning to mention any of the above — do NOT. "
            "Step over it and move to the next question."
        )
    else:
        lines = ["\n\n## Что владелец УЖЕ ИСКЛЮЧИЛ или ОГРАНИЧИЛ — НЕ возвращай эти темы:"]
        for r in retractions:
            lines.append(
                f"- УБРАНО ({r.get('type', '?')}): «{r.get('value', '')}» — "
                f"«{r.get('user_quote', '')}»"
            )
        for p in preferences:
            lines.append(
                f"- ПРЕДПОЧТЕНИЕ ({p.get('topic', '?')}): {p.get('value', '')} — "
                f"«{p.get('user_quote', '')}»"
            )
        lines.append(
            "Если планируешь упомянуть что-то из этого списка — НЕ упоминай. "
            "Перешагивай и переходи к следующему вопросу."
        )
    return "\n".join(lines)


class AdaptiveStateMachineWorkflow(Workflow):
    name = "v2"

    def __init__(self, llm: LLM):
        super().__init__(llm)

    # ---- public API ----

    def start(self, session: WorkflowSession) -> str:
        # If the agent layer already put us in SCREENING phase (the new fixed-survey
        # opener lives there, not here), don't emit the LLM-style opening — agent
        # has already populated the transcript with screening Q1.
        if session.state.conversation.phase == Phase.SCREENING:
            return session.transcript[-1]["content"] if session.transcript else ""
        lang = session.state.conversation.language
        opening = _OPENER[lang]
        session.append("assistant", opening, meta={
            "phase": "point_a",
            "kind": "opener",
            "internal_rationale": "приветствие + первый запрос (не используется при screening-фазе)",
        })
        session.state.conversation.phase = Phase.POINT_A
        return opening

    def respond(self, session: WorkflowSession, user_message: str) -> str:
        session.append("user", user_message)
        session.state.conversation.turn_count += 1

        # SPEC_REVIEW: PM-LLM classifies the user's reply and decides what to
        # do (confirm / correct / answer-question). Don't run the regular
        # extractor here — its user_confirmed_summary heuristic is brittle on
        # corrective replies. PM owns this phase end-to-end.
        if session.state.conversation.phase == Phase.SPEC_REVIEW:
            return self._handle_spec_review_turn(session, user_message)

        # ANALYSIS: the bot proposed solution shapes or asked a gap-fill
        # question. Classify the reply separately because it may select or
        # correct a solution rather than add a business fact.
        # Gate: once solution.shape is locked, fall through to normal flow so
        # next_phase() can advance us to RESOURCES on this turn.
        if (
            session.state.conversation.phase == Phase.ANALYSIS
            and session.state.solution.shape is None
        ):
            return self._handle_analysis_turn(session, user_message)

        # 0. If we were already in DEV_COVERAGE, record the user's reply.
        if session.state.conversation.phase == Phase.DEV_COVERAGE:
            self._record_dev_coverage_answer(session, user_message)

        # 1. Extract structured update from the last user message.
        self._extract(session)
        # 1b. Mini-pass: detect retractions / preferences and fold into
        #     session_memory + reconcile state with retracted facts.
        self._extract_session_memory(session, user_message)

        # 1c. LLM requirements are determined during ANALYSIS and propagated
        #     from the selected solution by _lock_solution.

        # 1d. Question-first responder. If the user asked something, we owe
        #     them an answer BEFORE the next workflow question. Skip in
        #     DEV_COVERAGE / SPEC_REVIEW (they have their own LLM handlers).
        question_answer: str | None = None
        if session.state.conversation.phase not in (Phase.DEV_COVERAGE, Phase.SPEC_REVIEW):
            question_answer = self._handle_user_question(session, user_message)

        # 2. Phase routing (deterministic).
        prev_phase = session.state.conversation.phase
        new_phase = session.state.next_phase()
        session.state.conversation.phase = new_phase
        phase_changed = new_phase != prev_phase

        # 3. If we just transitioned to DONE outside of SPEC_REVIEW path —
        #    legacy fallback (should rarely hit now).
        if new_phase == Phase.DONE and prev_phase != Phase.DONE:
            return self._emit_specs(session)

        # 3b. ANALYSIS — first entry (POINT_B → ANALYSIS transition). Run scanner +
        #     options generator and emit options for the owner. Subsequent owner
        #     replies in ANALYSIS are caught by the early return at top of respond().
        if new_phase == Phase.ANALYSIS and prev_phase != Phase.ANALYSIS:
            return self._handle_analysis_turn(session, user_message)

        # 4. NEW: DEV_COVERAGE — scanner LLM thinks "what would a developer ask?" and
        #    feeds one question back to the chat per turn until all gaps are closed.
        if new_phase == Phase.DEV_COVERAGE:
            return self._handle_dev_coverage_turn(session)

        # 5. Checkpoint summary on phase change or every CHECKPOINT_EVERY turns.
        turns_since_summary = (
            session.state.conversation.turn_count - session.state.conversation.last_summary_turn
        )
        should_checkpoint = phase_changed or (
            turns_since_summary >= CHECKPOINT_EVERY and not session.state.conversation.confirmed_summary
        )
        lang = session.state.conversation.language
        if should_checkpoint and any(self._has_any_facts(session.state)):
            text, meta = self._checkpoint_text(session, new_phase)
            text = self._prepend_answer(text, question_answer, lang=lang)
            text = self._postprocess_reply(session, text, skip_localize=True)
            session.append("assistant", text, meta=meta)
            session.state.conversation.last_summary_turn = session.state.conversation.turn_count
            return text

        # 6. Otherwise — ask the next question, targeted at open gaps.
        text, meta = self._ask_next(session)
        text = self._prepend_answer(text, question_answer, lang=lang)
        text = self._postprocess_reply(session, text)
        session.append("assistant", text, meta=meta)
        return text

    # ---- internals ----

    def _extract(self, session: WorkflowSession) -> None:
        # Compact transcript for the extractor: just the last 6 turns.
        recent = session.transcript[-6:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        resp = self.llm.complete(
            system=_extractor_system(),
            messages=messages,
            tools=[EXTRACTOR_TOOL],
            max_tokens=8000,  # generous — Gemini Flash cheap, capping caused truncation in cyrillic outputs
            temperature=0.0,
        )
        if resp.tool_use and resp.tool_use.get("name") == "update_state":
            _merge_patch(session.state, resp.tool_use.get("input") or {})

    def _ask_next(self, session: WorkflowSession) -> tuple[str, dict]:
        lang = session.state.conversation.language
        phase = session.state.conversation.phase
        next_field = spec_manifest.next_owner_field(session.state, phase)
        remaining = spec_manifest.owner_gaps(session.state, phase)

        recent = session.transcript[-8:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        resp = self.llm.complete(
            system=_asker_system(session.state, lang=lang),
            messages=messages,
            max_tokens=4000,  # generous — Flash cheap, prefer no truncation over micro-savings
            temperature=0.6,
        )
        _fallback = {"ru": "Расскажите подробнее, пожалуйста.", "en": "Tell me more, please."}
        text = sanitize(resp.text or _fallback.get(lang, _fallback["ru"]), lang=lang)
        meta = {
            "phase": phase.value,
            "target_field": next_field.name if next_field else None,
            "target_section": next_field.section if next_field else None,
            "owner_question_hint": next_field.owner_question if next_field else None,
            "remaining_gaps": [f.name for f in remaining],
            "kind": "asker",
            "internal_rationale": "asker LLM-вызов с подсказкой из manifest.next_owner_field",
        }
        return text, meta

    def _checkpoint_text(self, session: WorkflowSession, phase: Phase) -> tuple[str, dict]:
        # Pre-render guards. Each is a state-mutating script that runs over the
        # full transcript, hardening the summary against extractor drift:
        # - apply_no_ai_if_rejected: «никакого ИИ» → drop AI fields/quote
        # - filter_channels_to_user_history: drop channels owner never said
        # - recover_budget_from_history: scrape budget number if extractor
        #   missed it (Ольга said «5000», didn't land in state)
        apply_no_ai_if_rejected(session.state, session.transcript)
        filter_channels_to_user_history(session.state, session.transcript)
        recover_budget_from_history(session.state, session.transcript)
        # The summary is rendered deterministically from state — sanitize() is
        # regex-only. We deliberately do NOT route it through the LLM-based
        # localizer (would happen if 3+ EN brand names sit in a row in tools/
        # channels), since localization can rewrite line shape and the owner
        # asked for скриптовую, не LLM-решаемую Сводку.
        lang = session.state.conversation.language
        text = sanitize(SUMMARY_TEMPLATE.render(state=session.state, phase=phase, lang=lang), lang=lang)
        meta = {
            "phase": phase.value,
            "kind": "checkpoint_summary",
            "internal_rationale": "детерминированный пересказ — на смене фазы или каждые N ходов",
        }
        return text, meta

    # ---- Postprocess pipeline (English fragments, memory check) ----

    def _postprocess_reply(
        self, session: WorkflowSession, text: str, *, skip_localize: bool = False,
    ) -> str:
        """Final cleanup pass before bot text is appended/returned.

        1. Localize English phrases (3+ words) via LLM — instead of stripping
           them and losing meaning, translate to natural Russian. Skipped when
           skip_localize=True (summary-render path keeps the deterministic
           shape; we don't want an LLM rewrite there).
        2. Validate against session_memory — log violations onto the session
           rule_violations list. We don't strip on detection (false positives
           on legitimate brand mentions hurt more than the leak), but logging
           gives us a signal for the next iteration.
        """
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
            session.rule_violations.extend(
                f"session_memory: {v}" for v in memory_violations
            )
        return localized

    def _prepend_answer(self, text: str, answer: str | None, lang: str = "ru") -> str:
        """Glue the question-first answer (if any) onto the next workflow output."""
        if not answer:
            return text
        # Light sanitize so banned terms don't slip through the answer path.
        cleaned = sanitize(answer.strip(), lang=lang)
        if not cleaned:
            return text
        return f"{cleaned}\n\n{text}"

    # ---- Question-first responder ----

    def _handle_user_question(
        self, session: WorkflowSession, user_message: str,
    ) -> str | None:
        """Detect whether the user's reply contains a question. Return an
        user-friendly answer if so, otherwise None.

        Heuristic gate first: if the reply has no '?' and no question
        starters, skip the LLM call entirely.
        """
        if not user_message:
            return None
        msg_lower = user_message.lower().strip()
        cheap_signal = (
            "?" in msg_lower
            or msg_lower.startswith(("а ", "и ", "что ", "как ", "когда ", "почему ", "сколько ", "можно ", "это "))
            or " ли " in msg_lower
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
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=2000,
                temperature=0.2,
            )
            verdict = _parse_question_verdict(resp.text or "")
        except Exception:  # noqa: BLE001 — never block the chat on a question detector
            return None
        if not verdict.get("has_question"):
            return None
        return (verdict.get("answer") or "").strip() or None

    # ---- LLM-requirement detector ----

    def _maybe_detect_llm_requirement(self, session: WorkflowSession) -> None:
        """One-shot LLM call to mark whether this case actually needs an LLM.

        Trigger: point_b.desired_outcome is filled and llm_required is still
        None. Result lands on resources.llm_required (True / False), so the
        generated solution only includes AI when the identified need supports it.
        """
        resources = session.state.resources
        if resources.llm_required is not None:
            return
        outcome = (session.state.point_b.desired_outcome or "").strip()
        if not outcome:
            return
        payload = {
            "point_a": session.state.point_a.model_dump(mode="json"),
            "point_b": session.state.point_b.model_dump(mode="json"),
        }
        lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=DETECT_LLM_REQUIREMENT_PROMPT[lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=600,
                temperature=0.0,
            )
            verdict = _parse_llm_requirement(resp.text or "")
        except Exception:  # noqa: BLE001 — default to True on failure (safer to keep AI question)
            verdict = {"llm_required": True, "_error": "detector LLM call failed"}
        llm_req = verdict.get("llm_required")
        if isinstance(llm_req, bool):
            resources.llm_required = llm_req
            # If we just decided AI is not required, scrub any has_llm_key
            # that the extractor or PM-correction populated speculatively.
            if llm_req is False:
                resources.has_llm_key = None
                resources.llm_provider = None

    # ---- Session-memory extractor ----

    def _extract_session_memory(
        self, session: WorkflowSession, user_message: str,
    ) -> None:
        """Detect retractions / preferences in the user message and append to
        session_memory. Also reconciles state-side fields with retractions
        (drop retracted channels/tools, set has_llm_key=None on no_ai pref)."""
        if not user_message or len(user_message.strip()) < 2:
            return
        # Cheap heuristic: only invoke LLM when retraction/preference signals
        # are likely. Saves Gemini calls on plain factual answers.
        msg_lower = user_message.lower()
        signal_terms = (
            "не ", "нет", "убер", "удал", "без ", "не нужн", "не хочу", "не мой",
            "не пользу", "только ", "лень", "не дороже", "максимум", "минимум",
            "не дольше", "ничего лишнего", "проще", "не делай",
        )
        if not any(term in msg_lower for term in signal_terms):
            return

        payload = {
            "user_message": user_message,
            "state_summary": {
                "channels": session.state.point_a.channels,
                "tools_in_use": session.state.point_a.tools_in_use,
                "has_llm_key": session.state.resources.has_llm_key,
                "monthly_budget_rub": session.state.resources.monthly_budget_rub,
            },
        }
        _mem_lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=EXTRACT_MEMORY_PROMPT[_mem_lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=1500,
                temperature=0.0,
            )
            verdict = _parse_memory_verdict(resp.text or "")
        except Exception:  # noqa: BLE001
            return

        memory = session.state.conversation.session_memory
        turn = session.state.conversation.turn_count

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

    @staticmethod
    def _reconcile_retraction(session: WorkflowSession, entry: dict) -> None:
        """Remove the retracted entity from the corresponding state list."""
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
            # «не нужно ИИ» → drop AI markers
            if canon_value in {"ии", "llm", "chatgpt", "claude", "gemini"}:
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
        """Apply preference side-effects: e.g. no_ai → llm_required=False."""
        topic = (entry.get("topic") or "").lower()
        if topic == "no_ai":
            session.state.resources.llm_required = False
            session.state.resources.has_llm_key = None
            session.state.resources.llm_provider = None

    _ESTIMATE_PROMPTS: dict[str, str] = {
        "ru": (
            "Ты — solutions architect. На вход — JSON с собранной информацией "
            "о бизнес-проекте. Дай грубую вилку срока реализации одной "
            "короткой фразой без пояснений и без markdown. "
            "Примеры: '2-3 недели', 'около недели', '1-2 месяца', 'примерно 10 дней'. "
            "Только фраза, ничего больше."
        ),
        "en": (
            "You are a solutions architect. The input is a JSON with collected info "
            "about a business project. Reply with a rough delivery-time range as one "
            "short phrase, no explanations, no markdown. "
            "Examples: '2-3 weeks', 'about a week', '1-2 months', 'around 10 days'. "
            "Just the phrase, nothing else."
        ),
    }
    _ESTIMATE_FALLBACKS: dict[str, str] = {
        "ru": "уточним по ходу",
        "en": "we'll clarify as we go",
    }

    def _estimate_duration(self, state_json: str, lang: str = "ru") -> str:
        """Короткий доп. вызов: одна фраза с вилкой срока реализации."""
        fallback = self._ESTIMATE_FALLBACKS.get(lang, self._ESTIMATE_FALLBACKS["ru"])
        try:
            resp = self.llm.complete(
                system=self._ESTIMATE_PROMPTS.get(lang, self._ESTIMATE_PROMPTS["ru"]),
                messages=[{"role": "user", "content": state_json}],
                max_tokens=500,  # generous — Pro is smart, just a phrase; pinching causes truncation in cyrillic
                temperature=0.2,
            )
            txt = (resp.text or "").strip().strip(".").strip("«»\"'")
            return txt or fallback
        except Exception:
            return fallback

    # ========================================================================
    # ANALYSIS phase — owner picks one of 2-3 solution shapes before RESOURCES
    # ========================================================================
    ANALYSIS_GAP_FILL_CAP = 3

    def _handle_analysis_turn(
        self, session: WorkflowSession, user_message: str,
    ) -> str:
        """One iteration on the ANALYSIS phase.

        State machine within the phase:
        - First entry (no offered_options yet) — run scanner. Either ask a
          gap-fill question or jump straight to options-generation.
        - Subsequent entries (offered_options exist) — PM-LLM classifies
          the owner reply: pick / more_options / correct / question /
          gap_answer / unclear. Each branch dispatches accordingly.

        On `pick`: lock state.solution from the chosen option, propagate
        needs_ai → resources.llm_required, return an ack. Phase advancement
        happens on the NEXT turn via state.next_phase() (since solution.shape
        is now set, ANALYSIS is_phase_complete returns True).
        """
        sol = session.state.solution

        # Branch 1: owner is replying to options OR a gap-fill question.
        if sol.offered_options or sol.analysis_qa or self._has_recent_gap_question(session):
            verdict = self._analysis_pm_classify(session, user_message)
            intent = (verdict.get("intent") or "unclear").lower()

            if intent == "pick":
                idx = verdict.get("chosen_index")
                if isinstance(idx, int) and 1 <= idx <= len(sol.offered_options):
                    return self._lock_solution_and_ack(session, idx - 1)
                # Fall through if invalid index — re-render options.
                return self._reemit_options(session)

            if intent == "more_options":
                # Forget current options, regenerate with broader brief.
                sol.offered_options = []
                return self._generate_and_emit_options(session, more=True)

            if intent == "correct":
                idx = verdict.get("chosen_index")
                correction = (verdict.get("correction") or "").strip()
                if isinstance(idx, int) and 1 <= idx <= len(sol.offered_options) and correction:
                    return self._lock_solution_with_correction(session, idx - 1, correction)
                return self._reemit_options(session)

            if intent == "question":
                _qa_lang = session.state.conversation.language
                _q_fallback = {"ru": "Уточню.", "en": "Let me clarify."}
                _reprompt = {
                    "ru": "Подходит один из вариантов или нужны другие?",
                    "en": "Does one of the options work, or do you need different ones?",
                }
                answer = sanitize((verdict.get("answer") or "").strip(), lang=_qa_lang) or _q_fallback.get(_qa_lang, _q_fallback["ru"])
                # Append the answer + re-prompt to pick.
                follow_up = (
                    f"{answer}\n\n{_reprompt.get(_qa_lang, _reprompt['ru'])}"
                )
                text = self._postprocess_reply(session, follow_up)
                session.append("assistant", text, meta={
                    "phase": Phase.ANALYSIS.value,
                    "kind": "analysis_answer",
                    "internal_rationale": "PM-LLM ответил на вопрос владельца про опции",
                })
                return text

            if intent == "gap_answer":
                gap_answer = (verdict.get("gap_answer") or user_message).strip()
                last_q = self._last_gap_question(session)
                if last_q:
                    sol.analysis_qa.append({
                        "question": last_q,
                        "answer": gap_answer,
                        "gap": "",
                    })
                # Continue to scanner — it'll either ask another gap or propose options.
                return self._scanner_or_options(session)

            # intent == "unclear" — re-render options or re-ask gap question.
            if sol.offered_options:
                _unclear_prefix = {
                    "ru": "Не понял ответ. Выберите один из вариантов или скажите, что не так.",
                    "en": "I didn't catch that. Please pick one of the options or tell me what's off.",
                }
                return self._reemit_options(session, prefix=_unclear_prefix.get(
                    session.state.conversation.language, _unclear_prefix["ru"],
                ))
            return self._scanner_or_options(session)

        # Branch 2: first entry on ANALYSIS — scanner or options.
        return self._scanner_or_options(session)

    def _scanner_or_options(self, session: WorkflowSession) -> str:
        """Run the scanner; emit gap-fill or options accordingly."""
        sol = session.state.solution
        iteration = len(sol.analysis_qa) + 1
        if iteration > self.ANALYSIS_GAP_FILL_CAP:
            return self._generate_and_emit_options(session)

        verdict = self._analysis_run_scanner(session, iteration)
        action = (verdict.get("action") or "propose_options").lower()
        if action == "ask_gap" and iteration <= self.ANALYSIS_GAP_FILL_CAP:
            _gap_lang = session.state.conversation.language
            question = sanitize((verdict.get("gap_question") or "").strip(), lang=_gap_lang)
            if not question:
                # Scanner failed to give a question — fall through to options.
                return self._generate_and_emit_options(session)
            text = self._postprocess_reply(session, question)
            session.append("assistant", text, meta={
                "phase": Phase.ANALYSIS.value,
                "kind": "analysis_gap_question",
                "iteration": iteration,
                "gap_topic": (verdict.get("gap_topic") or "").strip()[:50],
                "internal_rationale": (
                    f"ANALYSIS scanner итерация {iteration}/{self.ANALYSIS_GAP_FILL_CAP}: ask_gap"
                ),
            })
            return text
        return self._generate_and_emit_options(session)

    def _analysis_run_scanner(
        self, session: WorkflowSession, iteration: int,
    ) -> dict:
        """Call scanner LLM. Returns parsed verdict or fallback to propose_options."""
        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [
                {"role": m["role"], "content": m["content"]}
                for m in session.transcript[-10:]
            ],
            "iteration": iteration,
        }
        _sc_lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=ANALYSIS_SCANNER_PROMPT[_sc_lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=2000,
                temperature=0.2,
            )
            return _parse_analysis_scanner_verdict(resp.text or "")
        except Exception:  # noqa: BLE001 — never block the chat
            return {"action": "propose_options", "_error": "scanner LLM failed"}

    def _analysis_generate_options(self, session: WorkflowSession) -> dict:
        """Call options-generator LLM. Returns parsed verdict (options + intro)."""
        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [
                {"role": m["role"], "content": m["content"]}
                for m in session.transcript[-10:]
            ],
        }
        _opt_lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=ANALYSIS_OPTIONS_PROMPT[_opt_lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=6000,
                temperature=0.4,
            )
            return _parse_analysis_options_verdict(resp.text or "")
        except Exception:  # noqa: BLE001
            return {"options": [], "intro": "", "_error": "options LLM failed"}

    def _generate_and_emit_options(
        self, session: WorkflowSession, *, more: bool = False,
    ) -> str:
        """Generate options, store on state, render owner-friendly message."""
        verdict = self._analysis_generate_options(session)
        options = verdict.get("options") or []
        if not options:
            # Fallback: emit a short message asking for direction.
            fallback = (
                "Я бы предложил пару вариантов, но информации пока маловато. "
                "Расскажите ещё немного — как клиент сейчас находит ваш бизнес "
                "и где удобнее всего получать ответы?"
            )
            text = self._postprocess_reply(session, fallback)
            session.append("assistant", text, meta={
                "phase": Phase.ANALYSIS.value,
                "kind": "analysis_options_fallback",
                "internal_rationale": "options-LLM не вернул вариантов — спросили направление",
            })
            return text

        session.state.solution.offered_options = options
        intro = (verdict.get("intro") or "").strip()
        message = self._render_options_message(
            options, intro, more=more,
            lang=session.state.conversation.language,
        )
        text = self._postprocess_reply(session, message, skip_localize=True)
        session.append("assistant", text, meta={
            "phase": Phase.ANALYSIS.value,
            "kind": "analysis_options",
            "options_count": len(options),
            "internal_rationale": (
                "ANALYSIS: показали 2-3 варианта решения, ждём выбора"
                + (" (по запросу 'другие варианты')" if more else "")
            ),
        })
        return text

    _OPTIONS_STRINGS: dict[str, dict[str, str]] = {
        "ru": {
            "more_intro": "Хорошо, вот другие варианты:",
            "fallback_intro": "Из того, что вы рассказали, есть пара путей. Выберите подходящий:",
            "shape_fallback": "вариант",
            "recommended_tag": " ⭐ обычно подходит лучше всего",
            "option_header": "Вариант",
            "budget_label": "бюджет",
            "timeline_label": "срок",
            "pick_prompt": "Скажите номер варианта (1, 2 или 3), либо напишите свои правки/вопросы.",
        },
        "en": {
            "more_intro": "Okay, here are other options:",
            "fallback_intro": "From what you've told me, there are a couple of paths. Pick the one that fits:",
            "shape_fallback": "option",
            "recommended_tag": " ⭐ usually the best fit",
            "option_header": "Option",
            "budget_label": "budget",
            "timeline_label": "timeline",
            "pick_prompt": "Say the option number (1, 2 or 3), or write your edits/questions.",
        },
    }

    def _render_options_message(
        self, options: list[dict], intro: str, *, more: bool = False, lang: str = "ru",
    ) -> str:
        """Owner-friendly bullet list of options. Numbered for picking."""
        s = self._OPTIONS_STRINGS.get(lang, self._OPTIONS_STRINGS["ru"])
        lines: list[str] = []
        if more:
            lines.append(s["more_intro"])
        elif intro:
            lines.append(intro)
        else:
            lines.append(s["fallback_intro"])
        lines.append("")
        for i, opt in enumerate(options, start=1):
            shape = (opt.get("shape") or "").strip() or f"{s['shape_fallback']} {i}"
            desc = (opt.get("description") or "").strip()
            budget = (opt.get("approx_budget_rub") or "").strip()
            timeline = (opt.get("approx_timeline") or "").strip()
            recommended = bool(opt.get("recommended"))
            tag = s["recommended_tag"] if recommended else ""
            lines.append(f"**{s['option_header']} {i} — {shape}{tag}**")
            if desc:
                lines.append(desc)
            meta_bits: list[str] = []
            if budget:
                meta_bits.append(f"{s['budget_label']} ~{budget}")
            if timeline:
                meta_bits.append(f"{s['timeline_label']} ~{timeline}")
            if meta_bits:
                lines.append("(" + "; ".join(meta_bits) + ")")
            lines.append("")
        lines.append(s["pick_prompt"])
        return "\n".join(lines).rstrip()

    def _analysis_pm_classify(
        self, session: WorkflowSession, user_message: str,
    ) -> dict:
        """Call PM-LLM to classify the owner's reply at ANALYSIS phase."""
        payload = {
            "offered_options": session.state.solution.offered_options,
            "transcript_tail": [
                {"role": m["role"], "content": m["content"]}
                for m in session.transcript[-10:]
            ],
            "user_message": user_message,
        }
        _pm_lang = session.state.conversation.language
        try:
            resp = self.llm.complete(
                system=ANALYSIS_PM_PROMPT[_pm_lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=2000,
                temperature=0.2,
            )
            return _parse_analysis_pm_verdict(resp.text or "")
        except Exception:  # noqa: BLE001
            return {"intent": "unclear", "_error": "pm LLM failed"}

    def _lock_solution_and_ack(
        self, session: WorkflowSession, opt_index: int,
    ) -> str:
        """Owner picked an option — write to state.solution, propagate to
        resources, return ack. Phase advances on next turn."""
        chosen = session.state.solution.offered_options[opt_index]
        sol = session.state.solution
        sol.shape = (chosen.get("shape") or "").strip() or f"вариант {opt_index + 1}"
        sol.shape_description = (chosen.get("description") or "").strip()
        sol.needs_ai = bool(chosen.get("needs_ai"))
        sol.needs_chat_ui = bool(chosen.get("needs_chat_ui"))
        sol.needs_external_integrations = bool(chosen.get("needs_external_integrations"))

        # Propagate to resources for the existing summary template + asker logic.
        session.state.resources.llm_required = sol.needs_ai
        if sol.needs_ai is False:
            # Belt-and-suspenders: clear stale AI fields the extractor may have
            # captured speculatively before ANALYSIS landed the verdict.
            session.state.resources.has_llm_key = None
            session.state.resources.llm_provider = None
            session.state.resources.llm_subscription_quote = None

        # Now advance phase and emit a checkpoint summary so the owner sees
        # «Договорились строить» plus the first RESOURCES question on the next turn.
        ack = (
            f"Окей, идём с этим: {sol.shape}.\n\n"
            "Теперь ещё немного про ресурсы — что есть и что готовы добавить."
        )
        text = self._postprocess_reply(session, ack)
        session.append("assistant", text, meta={
            "phase": Phase.ANALYSIS.value,
            "kind": "analysis_lock",
            "shape": sol.shape,
            "needs_ai": sol.needs_ai,
            "internal_rationale": (
                f"ANALYSIS lock: {sol.shape!r}, needs_ai={sol.needs_ai}, "
                f"needs_chat_ui={sol.needs_chat_ui}"
            ),
        })
        return text

    def _lock_solution_with_correction(
        self, session: WorkflowSession, opt_index: int, correction: str,
    ) -> str:
        """Owner picked option N but with a tweak. Lock the option but stitch
        the correction into shape_description (verbatim) so dev-spec downstream
        sees the full story."""
        chosen = session.state.solution.offered_options[opt_index]
        chosen_with_fix = dict(chosen)
        original_desc = (chosen.get("description") or "").strip()
        chosen_with_fix["description"] = (
            f"{original_desc} (с правкой владельца: {correction})"
            if original_desc else f"С правкой: {correction}"
        )
        session.state.solution.offered_options[opt_index] = chosen_with_fix
        return self._lock_solution_and_ack(session, opt_index)

    def _reemit_options(self, session: WorkflowSession, *, prefix: str = "") -> str:
        """Re-render the same offered_options (after question/unclear)."""
        options = session.state.solution.offered_options or []
        if not options:
            return self._scanner_or_options(session)
        message = self._render_options_message(
            options, intro=prefix or "",
            lang=session.state.conversation.language,
        )
        text = self._postprocess_reply(session, message, skip_localize=True)
        session.append("assistant", text, meta={
            "phase": Phase.ANALYSIS.value,
            "kind": "analysis_options_reemit",
            "internal_rationale": "перерендерили те же опции после вопроса/непонятного ответа",
        })
        return text

    def _has_recent_gap_question(self, session: WorkflowSession) -> bool:
        """True if the most recent assistant message was an analysis gap-fill
        question (so the next user reply is most likely the gap_answer)."""
        for m in reversed(session.transcript[:-1]):  # skip just-appended user turn
            if m.get("role") == "assistant":
                meta = m.get("meta") or {}
                return meta.get("kind") == "analysis_gap_question"
            if m.get("role") == "user":
                continue
        return False

    def _last_gap_question(self, session: WorkflowSession) -> str | None:
        for m in reversed(session.transcript[:-1]):
            if m.get("role") == "assistant":
                meta = m.get("meta") or {}
                if meta.get("kind") == "analysis_gap_question":
                    return m.get("content") or None
                # Other assistant message — stop walking.
                return None
        return None

    def _handle_dev_coverage_turn(self, session: WorkflowSession) -> str:
        """One iteration of the dev-coverage scanner loop.

        Calls the scanner LLM with current state + transcript_tail. If it returns
        ``complete=true`` (or the safety cap is hit), set the flag and immediately
        re-route through the normal SPEC_REVIEW path so the user sees a checkpoint
        summary on the same turn. Otherwise emit the scanner's question to chat.
        """
        qa_list = session.state.resources.dev_coverage_qa
        iteration = len(qa_list) + 1

        # Hard cap: stop after N iterations even if scanner keeps finding gaps.
        if iteration > DEV_COVERAGE_MAX_ITERATIONS:
            return self._proceed_after_dev_coverage_complete(session)

        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [
                {"role": m["role"], "content": m["content"]}
                for m in session.transcript[-20:]
            ],
            "iteration": iteration,
        }

        _dc_lang = session.state.conversation.language
        verdict: dict
        try:
            resp = self.llm.complete(
                system=SPEC_COVERAGE_SCAN_PROMPT[_dc_lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=4000,  # generous — scanner JSON with question + rationale, never want truncation
                temperature=0.3,
            )
            verdict = _parse_scan_verdict(resp.text or "")
        except Exception:  # noqa: BLE001 — scanner failure must not block the chat
            verdict = {"complete": True, "_error": "scanner LLM call failed"}

        if verdict.get("complete") is True:
            return self._proceed_after_dev_coverage_complete(session)

        _dc_fallback = {
            "ru": "Уточните, пожалуйста — какой формат для вас удобнее?",
            "en": "Could you clarify — what format works best for you?",
        }
        question = sanitize(
            (verdict.get("question") or "").strip()
            or _dc_fallback.get(_dc_lang, _dc_fallback["ru"]),
            lang=_dc_lang,
        )
        question = self._postprocess_reply(session, question)
        rationale = (verdict.get("rationale") or "").strip()[:200]
        session.append("assistant", question, meta={
            "phase": Phase.DEV_COVERAGE.value,
            "kind": "dev_coverage_question",
            "iteration": iteration,
            "rationale": rationale,
            "internal_rationale": (
                f"DEV_COVERAGE scanner итерация {iteration}/{DEV_COVERAGE_MAX_ITERATIONS}: {rationale}"
            ),
        })
        return question

    def _record_dev_coverage_answer(
        self, session: WorkflowSession, user_message: str,
    ) -> None:
        """Walk back through transcript to find the most recent scanner question
        that hasn't been paired with an answer yet, and append the Q+A to
        ``state.resources.dev_coverage_qa``.

        Idempotent — if the last scanner question already has a pair (because
        we're processing a non-answer message like a comment-only turn), we
        skip rather than duplicate.
        """
        existing_qa = session.state.resources.dev_coverage_qa
        existing_questions = {entry.get("question") for entry in existing_qa}
        last_q: str | None = None
        last_rationale = ""
        for m in reversed(session.transcript[:-1]):  # skip the just-appended user turn
            meta = m.get("meta") or {}
            if m.get("role") == "assistant" and meta.get("kind") == "dev_coverage_question":
                q = m.get("content") or ""
                if q in existing_questions:
                    return  # already paired
                last_q = q
                last_rationale = meta.get("rationale") or ""
                break
            if m.get("role") == "user":
                # we only care about the scanner question that immediately preceded the user msg
                continue
        if not last_q:
            return
        session.state.resources.dev_coverage_qa.append({
            "question": last_q,
            "answer": user_message,
            "rationale": last_rationale,
        })

    def _handle_spec_review_turn(
        self, session: WorkflowSession, user_message: str,
    ) -> str:
        """SPEC_REVIEW handler.

        - Front-end button «да, вопросов нет» sends `CONFIRM_BUTTON_TEXT`
          verbatim → deterministic confirm path → emit_specs.
        - Anything else (free-form text) → PM-LLM classifies the reply
          (confirm / correct / question), applies corrections via patch, or
          answers the question and re-prompts.

        """
        normalized = (user_message or "").strip().rstrip(".!").lower()
        if normalized == CONFIRM_BUTTON_TEXT.lower():
            session.state.conversation.confirmed_summary = True
            return self._emit_specs(session)

        # PM-LLM classification.
        payload = {
            "state": session.state.model_dump(mode="json"),
            "transcript_tail": [
                {"role": m["role"], "content": m["content"]}
                for m in session.transcript[-12:]
            ],
            "user_message": user_message,
        }
        _sr_lang = session.state.conversation.language
        verdict: dict
        try:
            resp = self.llm.complete(
                system=PM_REVIEW_PROMPT[_sr_lang],
                messages=[{
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False, indent=2),
                }],
                max_tokens=2500,
                temperature=0.2,
            )
            verdict = _parse_pm_verdict(resp.text or "")
        except Exception:  # noqa: BLE001 — PM failure must not block the chat
            _pm_err_msg = {
                "ru": "Простите, не понял. Можете переформулировать?",
                "en": "Sorry, I didn't follow. Could you rephrase?",
            }
            verdict = {
                "intent": "question",
                "answer": _pm_err_msg.get(_sr_lang, _pm_err_msg["ru"]),
                "_error": "pm LLM call failed",
            }

        intent = (verdict.get("intent") or "question").lower()

        if intent == "confirm":
            session.state.conversation.confirmed_summary = True
            return self._emit_specs(session)

        if intent == "correct":
            patch = verdict.get("patch") or {}
            if patch:
                _apply_pm_patch(session.state, patch)
            _ack_fallback = {"ru": "Поправил.", "en": "Fixed."}
            ack = sanitize(
                (verdict.get("ack") or _ack_fallback.get(_sr_lang, _ack_fallback["ru"])).strip(),
                lang=_sr_lang,
            )
            summary, summary_meta = self._checkpoint_text(session, Phase.SPEC_REVIEW)
            text = self._postprocess_reply(session, f"{ack}\n\n{summary}", skip_localize=True)
            session.append("assistant", text, meta={
                "phase": Phase.SPEC_REVIEW.value,
                "kind": "pm_correction_ack",
                "internal_rationale": "PM-LLM applied correction patch + re-confirmed summary",
            })
            session.state.conversation.last_summary_turn = (
                session.state.conversation.turn_count
            )
            return text

        # intent == "question" (or unknown — default safe path)
        _ans_fallback = {
            "ru": "Хороший вопрос — давайте уточню.",
            "en": "Good question — let me clarify.",
        }
        _reprompt_confirm = {
            "ru": (
                "Если больше вопросов нет — нажмите «да, вопросов нет», "
                "или напишите ещё правки/вопросы."
            ),
            "en": (
                "If you have no more questions — tap «yes, no questions» / type that phrase, "
                "or write more edits/questions."
            ),
        }
        answer = sanitize(
            (verdict.get("answer") or _ans_fallback.get(_sr_lang, _ans_fallback["ru"])).strip(),
            lang=_sr_lang,
        )
        text = self._postprocess_reply(session, (
            f"{answer}\n\n"
            + _reprompt_confirm.get(_sr_lang, _reprompt_confirm["ru"])
        ))
        session.append("assistant", text, meta={
            "phase": Phase.SPEC_REVIEW.value,
            "kind": "pm_answer",
            "internal_rationale": "PM-LLM answered owner's question, re-prompting confirm",
        })
        return text

    def _proceed_after_dev_coverage_complete(self, session: WorkflowSession) -> str:
        """Mark coverage complete and immediately advance to SPEC_REVIEW with a
        deterministic checkpoint summary, so the owner sees what's locked and
        gets the «всё так?» prompt without an extra empty turn."""
        session.state.conversation.dev_coverage_complete = True
        new_phase = session.state.next_phase()
        session.state.conversation.phase = new_phase
        text, meta = self._checkpoint_text(session, new_phase)
        text = self._postprocess_reply(session, text, skip_localize=True)
        meta["internal_rationale"] = (
            "DEV_COVERAGE завершён (scanner=complete или 8-iter cap) → "
            + meta.get("internal_rationale", "")
        )
        session.append("assistant", text, meta=meta)
        session.state.conversation.last_summary_turn = session.state.conversation.turn_count
        return text

    def _emit_specs(self, session: WorkflowSession) -> str:
        lang = session.state.conversation.language
        state_dump = session.state.model_dump(mode="json")
        state_json = json.dumps(state_dump, ensure_ascii=False, indent=2)
        transcript_tail = session.transcript[-20:]
        skill_content = load_skill("dual-spec-writer")

        # Бизнес-версия: только state, sanitize на выходе. Имя «разработчик» в финал
        # точно не должно просочиться (поэтому sanitize, а не только промпт).
        biz_raw = self.llm.complete(
            system=BUSINESS_SPEC_PROMPT[lang],
            messages=[{"role": "user", "content": state_json}],
            max_tokens=16000,  # generous — long-form biz spec, Flash cheap, quality > cost
            temperature=0.3,
        ).text
        biz = sanitize(biz_raw, lang=lang)

        # Dev-версия: state + skill + хвост транскрипта. dev_coverage_qa уже внутри
        # state.resources, так что промпт читает его оттуда. NO pending_decisions —
        # все архитектурные развилки закрыты ранее на DEV_COVERAGE фазе.
        dev_payload = {
            "state": state_dump,
            "transcript_tail": transcript_tail,
        }
        dev_raw = self._generate_dev_spec(dev_payload, skill_content, lang=lang)
        # На dev_raw применяем только подмену "разработчик" → "исполнитель",
        # но НЕ полный sanitize: технические термины (API, webhook, endpoint)
        # в тех-плане легитимны.
        dev = strip_dev_mentions(dev_raw)

        violations = check_variant_blocks(dev, lang=lang)
        if violations:
            session.rule_violations.extend(violations)

        session.business_spec = biz
        session.dev_spec = dev
        session.state.conversation.phase = Phase.DONE
        if session.state.conversation.ended_at is None:
            session.state.conversation.ended_at = datetime.now(timezone.utc)

        # Короткий ack для владельца — спеки в чат НЕ выводим, они уезжают в
        # Firestore/Sheets/Drive для нашей работы. Владельцу — спасибо + оценка
        # времени + CTA на оплату.
        estimate = self._estimate_duration(state_json, lang=lang)
        session.state.conversation.estimate_text = estimate
        ack = self._postprocess_reply(session, FINAL_ACK_TEMPLATE[lang].format(estimate=estimate))
        session.append("assistant", ack, meta={
            "phase": "done",
            "kind": "spec_emission",
            "internal_rationale": (
                "владелец подтвердил пересказ → две спеки сохранены внутри (биз+дев), "
                "владельцу отправлен короткий ack + оценка времени + CTA на оплату"
            ),
        })
        return ack

    def _generate_dev_spec(self, dev_payload: dict, skill_content: str, lang: str = "ru") -> str:
        """Run the dev-spec LLM call. If the result contains forbidden variant
        blocks (`check_variant_blocks`), retry once with a corrective directive.

        Returns the final spec text (best-effort: even after retry, we ship
        whatever we got and let `_emit_specs` log violations to
        `session.rule_violations`).
        """
        base_system = DEV_SPEC_PROMPT[lang] + "\n\n---\n\n# Skill: dual-spec-writer\n\n" + skill_content
        user_msg = json.dumps(dev_payload, ensure_ascii=False, indent=2)

        first = self.llm.complete(
            system=base_system,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=20000,  # generous — long-form technical spec, never truncate
            temperature=0.2,
        ).text or ""

        if not check_variant_blocks(first, lang=lang):
            return first

        # Retry once with a stronger anti-variant directive prepended.
        _retry_directives: dict[str, str] = {
            "ru": (
                "\n\n# КРИТИЧНОЕ ПРАВИЛО (повторно)\n"
                "Предыдущий черновик содержал запрещённые блоки «На выбор», «Вариант A/B/C» "
                "или «Рекомендация». Все архитектурные развилки УЖЕ закрыты заказчиком "
                "(см. state.resources.dev_coverage_qa). Перепиши спеку: каждая секция = "
                "ОДИН конкретный ответ, без альтернатив. НЕ используй фразы «На выбор», "
                "«Вариант A/B/C», «Рекомендация». Если в state нет явного ответа — выбери "
                "разумный default и обоснуй одной строкой."
            ),
            "en": (
                "\n\n# CRITICAL RULE (repeated)\n"
                "The previous draft contained forbidden 'Option A/B/C', 'Variant', or 'Recommendation' blocks. "
                "All architectural decisions are ALREADY locked by the customer (see state.resources.dev_coverage_qa). "
                "Rewrite the spec: every section = ONE concrete answer, no alternatives. DO NOT use 'Option A/B/C', "
                "'Variant', or 'Recommendation'. If state has no explicit answer, pick a reasonable default and "
                "justify it in one line."
            ),
        }
        retry_directive = _retry_directives.get(lang, _retry_directives["ru"])
        retry = self.llm.complete(
            system=base_system + retry_directive,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=20000,  # generous — long-form technical spec, never truncate
            temperature=0.1,
        ).text or first
        return retry

    @staticmethod
    def _has_any_facts(state: WorkflowState) -> tuple[bool, ...]:
        return (
            state.coverage_a() > 0,
            state.coverage_b() > 0,
            state.coverage_resources() > 0,
        )


# ---- Parsing helpers ----

def _apply_pm_patch(state: WorkflowState, patch: dict) -> None:
    """Apply a PM-issued correction patch.

    Differs from _merge_patch (extractor): product-manager patches replace
    list values instead of appending, so explicit removals are preserved.
    """
    for section_key in ("point_a", "point_b", "resources"):
        section_patch = patch.get(section_key) or {}
        section = getattr(state, section_key, None)
        if section is None:
            continue
        for k, v in section_patch.items():
            if not hasattr(section, k):
                continue
            # Replace lists wholesale; for None set to default ([] for list,
            # None for scalar). For scalars/bool/int just set.
            existing = getattr(section, k)
            if isinstance(existing, list):
                setattr(section, k, list(v) if isinstance(v, list) else [])
            else:
                setattr(section, k, v)
    if patch.get("user_confirmed_summary"):
        state.conversation.confirmed_summary = True


def _parse_pm_verdict(text: str) -> dict:
    """Parse PM_REVIEW_PROMPT output. On failure default to a polite
    «question» intent so the chat doesn't break."""
    if not text:
        return {"intent": "question", "answer": "Простите, не уловил. Повторите, пожалуйста?"}
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
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict) and parsed.get("intent"):
                return parsed
        except json.JSONDecodeError:
            pass
    # Fallback: extract intent + answer via regex if JSON malformed.
    intent_match = re.search(r'"intent"\s*:\s*"(\w+)"', raw)
    answer_match = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if intent_match:
        out: dict = {"intent": intent_match.group(1), "_recovered": True}
        if answer_match:
            out["answer"] = answer_match.group(1)
        return out
    return {
        "intent": "question",
        "answer": "Простите, что-то не уловил. Скажите ещё раз — это правка или вопрос?",
        "_parse_error": True,
    }


def _parse_question_verdict(text: str) -> dict:
    """Parse ANSWER_USER_QUESTION_PROMPT output. On failure, default to
    `{"has_question": false}` so we don't accidentally inject garbage."""
    if not text:
        return {"has_question": False}
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
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    # Truncation-tolerant fallback.
    if re.search(r'"has_question"\s*:\s*false', raw, re.IGNORECASE):
        return {"has_question": False}
    answer_match = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
    if answer_match:
        return {"has_question": True, "answer": answer_match.group(1)}
    return {"has_question": False, "_parse_error": True}


def _parse_llm_requirement(text: str) -> dict:
    """Parse DETECT_LLM_REQUIREMENT_PROMPT output."""
    if not text:
        return {"llm_required": True, "_error": "empty response"}
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
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict) and isinstance(parsed.get("llm_required"), bool):
                return parsed
        except json.JSONDecodeError:
            pass
    if re.search(r'"llm_required"\s*:\s*true', raw, re.IGNORECASE):
        return {"llm_required": True}
    if re.search(r'"llm_required"\s*:\s*false', raw, re.IGNORECASE):
        return {"llm_required": False}
    return {"llm_required": True, "_parse_error": True}


def _parse_memory_verdict(text: str) -> dict:
    """Parse EXTRACT_MEMORY_PROMPT output. Defaults to empty lists."""
    if not text:
        return {"retractions": [], "preferences": []}
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
    if start != -1 and end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {"retractions": [], "preferences": [], "_parse_error": True}


def _parse_scan_verdict(text: str) -> dict:
    """Parse the SPEC_COVERAGE_SCAN_PROMPT output, tolerating fenced code blocks,
    stray text around JSON, AND truncated output where the closing brace got
    eaten by max_tokens. Truncation is the dominant failure mode on Gemini's
    cyrillic tokenizer — when rationale runs long, the closing `}"` disappears,
    standard JSON parsers fail, and we'd silently default to complete=true and
    skip DEV_COVERAGE. So we extract the question via regex first as a fallback.

    On any unrecoverable parse failure, default to {"complete": true} so the
    workflow advances rather than getting stuck.
    """
    if not text:
        return {"complete": True, "_error": "empty scanner response"}
    raw = text.strip()
    if raw.startswith("```"):
        first_nl = raw.find("\n")
        if first_nl != -1:
            raw = raw[first_nl + 1:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    start = raw.find("{")
    if start == -1:
        return {"complete": True, "_error": "no JSON object in scanner response"}

    # Try strict parse first (well-formed JSON).
    end = raw.rfind("}")
    if end > start:
        try:
            parsed = json.loads(raw[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass  # fall through to truncation-tolerant parse

    # Truncation-tolerant: explicit complete=true wins.
    if re.search(r'"complete"\s*:\s*true', raw):
        return {"complete": True}

    # Otherwise look for a question substring. Gemini opens with
    # `"complete": false, "question": "..."` — pull the first quoted string
    # after `"question":`.
    question_match = re.search(
        r'"question"\s*:\s*"((?:[^"\\]|\\.)*)"',
        raw,
    )
    rationale_match = re.search(
        r'"rationale"\s*:\s*"((?:[^"\\]|\\.)*)"?',
        raw,
    )
    if question_match:
        return {
            "complete": False,
            "question": question_match.group(1),
            "rationale": (rationale_match.group(1) if rationale_match else "truncated"),
            "_recovered_from_truncation": True,
        }

    return {"complete": True, "_error": "scanner JSON parse failed irrecoverably"}


def _extract_json_object(text: str) -> dict | None:
    """Common helper: strip code-fences, return first parsed JSON object or None."""
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
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_analysis_scanner_verdict(text: str) -> dict:
    """Parse ANALYSIS_SCANNER_PROMPT output. Default to propose_options on
    any failure so we don't get stuck in gap-fill loops."""
    parsed = _extract_json_object(text)
    if parsed is None:
        return {"action": "propose_options", "_error": "scanner parse failed"}
    action = (parsed.get("action") or "").lower()
    if action not in ("ask_gap", "propose_options"):
        return {"action": "propose_options", "_error": "unknown action"}
    return parsed


def _parse_analysis_options_verdict(text: str) -> dict:
    """Parse ANALYSIS_OPTIONS_PROMPT output. Returns {options, intro}."""
    parsed = _extract_json_object(text)
    if parsed is None:
        return {"options": [], "intro": "", "_error": "options parse failed"}
    options = parsed.get("options") or []
    if not isinstance(options, list):
        return {"options": [], "intro": "", "_error": "options not a list"}
    cleaned: list[dict] = []
    for opt in options:
        if not isinstance(opt, dict):
            continue
        cleaned.append({
            "shape": str(opt.get("shape") or "").strip()[:200],
            "description": str(opt.get("description") or "").strip()[:600],
            "needs_ai": bool(opt.get("needs_ai")),
            "needs_chat_ui": bool(opt.get("needs_chat_ui")),
            "needs_external_integrations": bool(opt.get("needs_external_integrations")),
            "approx_budget_rub": str(opt.get("approx_budget_rub") or "").strip()[:80],
            "approx_timeline": str(opt.get("approx_timeline") or "").strip()[:80],
            "recommended": bool(opt.get("recommended")),
        })
    return {
        "options": cleaned[:3],  # cap at 3
        "intro": str(parsed.get("intro") or "").strip()[:400],
    }


def _parse_analysis_pm_verdict(text: str) -> dict:
    """Parse ANALYSIS_PM_PROMPT output. Default intent='unclear' on failure."""
    parsed = _extract_json_object(text)
    if parsed is None:
        return {"intent": "unclear", "_error": "pm parse failed"}
    intent = (parsed.get("intent") or "").lower()
    if intent not in (
        "pick", "more_options", "correct", "question", "gap_answer", "unclear",
    ):
        return {"intent": "unclear", "_error": "unknown intent"}
    return parsed
