"""V3 — Screening + Mom-Test + JTBD Funnel.

Live-test learning: open invitation "tell me about your business" gives users
too much room to confabulate. Real workflow now starts with TWO multiple-choice
screening questions (sector + scale), then a concrete time-eater question with
options, only THEN moves to extraction-driven structured phase.

Why this is different from V2: V2 starts asking with options for state slots
(channels, tools, etc.). V3 first ANCHORS sector/scale/time-eater so the user's
mental frame is concrete from turn 1, then digs into what they actually do —
not what they say they do. Research (Mom Test, NN/g) and product feedback both
say users self-deceive on open invitations; structured anchoring kills that.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..llm import LLM
from ..state import Phase, WorkflowState
from .base import Workflow, WorkflowSession
from .common_prompts import (
    BUSINESS_SPEC_PROMPT,
    CONVERSATIONAL_STYLE,
    DEV_SPEC_PROMPT,
    SUMMARY_TEMPLATE,
)

# V3 is an experimental workflow not exposed via web. Pin all dict prompts to RU.
_LANG: str = "ru"
from .v2_adaptive import EXTRACTOR_TOOL, _extractor_system, _merge_patch

SCREENING_TURNS = 5   # turns 1..5 are scripted screening; turn 6+ goes to extraction
CHECKPOINT_EVERY = 5

# Q1 is the opening message itself (turn=0 emit, user reply at turn=1).
OPENING_QUESTION = (
    "Привет. Я могу помочь сократить время работы вашей и ваших сотрудников "
    "и увеличить прибыль. Ответьте на несколько коротких вопросов — это займёт "
    "не более 5 минут.\n\n"
    "Сколько вам лет:\n"
    "— до 25\n"
    "— 25–35\n"
    "— 35–45\n"
    "— 45–55\n"
    "— старше 55"
)

SCREENING_Q2 = (
    "Ваш пол:\n"
    "— женский\n"
    "— мужской\n"
    "— не уточняю"
)

SCREENING_Q3 = (
    "В каком секторе работаете:\n"
    "— услуги (салон / клиника / мастерская / частный мастер)\n"
    "— товары (магазин / маркетплейс)\n"
    "— онлайн-обучение или контент\n"
    "— производство\n"
    "— B2B / корпоративные продажи\n"
    "— другое (опишите коротко)"
)

SCREENING_Q4 = (
    "Сколько человек в команде, включая вас:\n"
    "— я один\n"
    "— 2–5\n"
    "— 6–15\n"
    "— 16–50\n"
    "— больше 50"
)

SCREENING_Q5 = (
    "Что в обычный день занимает больше всего времени у вас или команды:\n"
    "— общение с клиентами (входящие, заявки, ответы)\n"
    "— продажи (звонки, переговоры, ведение сделок)\n"
    "— производство / выполнение заказов\n"
    "— административные задачи (бухгалтерия, документы, склад)\n"
    "— реклама / маркетинг / контент\n"
    "— что-то ещё (напишите)"
)


_SCREENING_QUESTIONS = [SCREENING_Q2, SCREENING_Q3, SCREENING_Q4, SCREENING_Q5]


def _next_screening_question(turn_count: int) -> str | None:
    """Q at turn N is emitted *after* the user answered turn N's previous question.

    Mapping:
      turn_count=1 → SCREENING_Q2 (we just got age, ask gender)
      turn_count=2 → SCREENING_Q3 (sector)
      turn_count=3 → SCREENING_Q4 (team)
      turn_count=4 → SCREENING_Q5 (time-eater)
      turn_count>=5 → None (screening done)
    """
    if 1 <= turn_count <= 4:
        return _SCREENING_QUESTIONS[turn_count - 1]
    return None


# --- Deterministic parsers for screening answers ---


def _parse_age(text: str) -> str | None:
    t = text.lower()
    if "до 25" in t or "<25" in t or "моложе 25" in t:
        return "<25"
    if "25" in t and "35" in t:
        return "25-35"
    if "35" in t and "45" in t:
        return "35-45"
    if "45" in t and "55" in t:
        return "45-55"
    if "55" in t or "старше" in t:
        return "55+"
    return None


def _parse_gender(text: str) -> str | None:
    t = text.lower()
    if "женск" in t or t.strip() == "ж":
        return "female"
    if "мужск" in t or t.strip() == "м":
        return "male"
    if "не уточн" in t or "не указыв" in t:
        return "not_specified"
    return None


def _parse_sector(text: str) -> str | None:
    t = text.lower()
    if "услуг" in t or "салон" in t or "клиник" in t or "мастер" in t:
        return "services"
    if "товар" in t or "магазин" in t or "маркетплейс" in t:
        return "goods"
    if "обучен" in t or "контент" in t or "курс" in t or "онлайн-обучен" in t:
        return "online_education"
    if "производств" in t or "произво" in t:
        return "production"
    if "b2b" in t or "корпоратив" in t:
        return "b2b"
    return "other"


def _parse_team_size(text: str) -> int | None:
    t = text.lower()
    if "один" in t or t.strip() == "1" or "я сам" in t:
        return 1
    if "2" in t and ("5" in t or "–5" in t or "-5" in t):
        return 3  # midpoint
    if "6" in t and "15" in t:
        return 10
    if "16" in t and "50" in t:
        return 30
    if "50" in t or "больше" in t:
        return 75
    return None


def _parse_time_eater(text: str) -> str | None:
    t = text.lower()
    if "клиент" in t or "общени" in t or "входящ" in t or "заявк" in t:
        return "client_comms"
    if "продаж" in t or "звонк" in t or "переговор" in t:
        return "sales"
    if "производств" in t or "заказ" in t:
        return "production"
    if "админ" in t or "бухгалт" in t or "документ" in t:
        return "admin"
    if "реклам" in t or "маркетинг" in t or "контент" in t:
        return "marketing"
    return "other"


def _apply_screening_answer(state, turn_count: int, user_text: str) -> None:
    """Map the user's reply to the screening question that was just asked."""
    if turn_count == 1:  # answered Q1: age
        v = _parse_age(user_text)
        if v:
            state.profile.age_range = v
    elif turn_count == 2:  # gender
        v = _parse_gender(user_text)
        if v:
            state.profile.gender = v
    elif turn_count == 3:  # sector
        v = _parse_sector(user_text)
        if v:
            state.profile.sector = v
            # also seed a coarse business_type
            if not state.point_a.business_type:
                state.point_a.business_type = {
                    "services": "услуги",
                    "goods": "товары",
                    "online_education": "онлайн-обучение / контент",
                    "production": "производство",
                    "b2b": "B2B",
                    "other": "другое",
                }.get(v, "другое")
    elif turn_count == 4:  # team size
        v = _parse_team_size(user_text)
        if v is not None:
            state.point_a.team_size = v
    elif turn_count == 5:  # time eater
        v = _parse_time_eater(user_text)
        if v:
            state.profile.time_eater = v


JTBD_TRANSITION = (
    "\nХорошо. Теперь несколько уточнений, по одному вопросу за раз.\n"
)

MOMTEST_SYSTEM = (
    CONVERSATIONAL_STYLE[_LANG]
    + """\n\n## Сейчас особый режим: Mom Test (мягкий вариант).
Принципы Rob Fitzpatrick, адаптированные:
1. Если человек назвал направление ("хочу автоматизировать X") — копай в это направление.
   Узнавай, как X *сейчас* у них работает: канал, инструменты, кто делает, объёмы.
2. Если человек назвал конкретную задачу — узнавай частоту и что уже пробовали.
3. Не используй эмоциональные формулировки ("болит", "бесит", "напрягает", "достало").
   Спрашивай нейтрально и фактически.
4. Не насилуй поиском конкретного случая, если человек говорит "просто хочу X" — это
   нормальная позиция, не каждый клиент приходит с болью.
5. Слушай больше, чем предлагай.

Хорошие нейтральные follow-up'ы:
- "Где это сейчас происходит — какие каналы?"
- "Кто этим сейчас занимается?"
- "Что уже пробовали для этого?"
- "Опишите типичный путь — клиент пишет, дальше что?"
- "Сколько обращений в день/неделю примерно?"
- "Что именно хотите автоматизировать в этом процессе?"

Запрещено: "что бесит", "что отнимает нервы", "что больше всего напрягает", "болит".
Можно: "что съедает время", "что хотите ускорить", "что хотите упростить".

ЗАДАЧА: задай ОДИН follow-up к последнему ответу. Никаких меню. \
Без преамбул. Нейтрально, фактически."""
)


def _asker_system_v3(state: WorkflowState) -> str:
    """Like V2 asker but adds JTBD timeline framing."""
    phase = state.conversation.phase
    gaps = state.open_gaps(phase)
    phase_focus = {
        Phase.POINT_A: (
            "Точка А. Закрывай пробелы из списка по одному вопросу за раз. "
            "Можно с вариантами (3–4) — они нужны для подтверждения, не для разведки."
        ),
        Phase.POINT_B: (
            "Точка Б. Внутренний таймлайн (не упоминай вслух эти термины): "
            "first_thought → passive_looking → active_looking → deciding. "
            "Если ещё не выяснил, что должно случиться, чтобы они запустили — спроси про это "
            "обычными словами ('что должно сложиться, чтобы вы реально сели и запустили?'). "
            "Обязательно вытащи 'что НЕ делаем'."
        ),
        Phase.RESOURCES: (
            "Ресурсы. ОДИН вопрос за реплику. Не пакетируй (бюджет/ключ/поддержка/out-of-scope) в одно сообщение — "
            "это ломает темп для нетехнаря. Сначала закрой бюджет, дождись ответа, потом ключ, и так далее."
        ),
        Phase.SPEC_REVIEW: (
            "Финальный пересказ. Подтверди и спроси 'всё так?' — после 'да' выдашь спеки."
        ),
    }.get(phase, "")
    return (
        CONVERSATIONAL_STYLE[_LANG]
        + "\n\n## Текущая фаза\n" + phase_focus
        + f"\n\n## Пробелы: {', '.join(gaps) if gaps else 'нет — фаза готова'}"
        + "\n\n## State (только для тебя):\n"
        + json.dumps(state.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n\nЗадай ОДИН вопрос. Без преамбул. Если уместно — варианты. "
        + "Никогда не используй жаргон вроде 'JTBD', 'API', 'webhook' в реплике "
        + "пользователю без объяснения одной короткой фразой."
    )


class MomTestJTBDWorkflow(Workflow):
    name = "v3"

    def __init__(self, llm: LLM):
        super().__init__(llm)

    # ---- public API ----

    def start(self, session: WorkflowSession) -> str:
        session.append("assistant", OPENING_QUESTION)
        session.state.conversation.phase = Phase.POINT_A
        return OPENING_QUESTION

    def respond(self, session: WorkflowSession, user_message: str) -> str:
        session.append("user", user_message)
        session.state.conversation.turn_count += 1

        # Phase 0: Scripted screening — turns 1..SCREENING_TURNS-1 emit hardcoded
        # multi-choice questions; the last screening turn hands control to extractor.
        if session.state.conversation.turn_count <= SCREENING_TURNS:
            _apply_screening_answer(
                session.state, session.state.conversation.turn_count, user_message
            )
            text = _next_screening_question(session.state.conversation.turn_count)
            if text is not None:
                session.append("assistant", text)
                return text
            # Fall through to extractor at turn == SCREENING_TURNS (last screening reply)

        # Once screening is done, behave like V2: extract → route → ask/summarize.
        self._extract(session)

        # V3 doesn't implement the DEV_COVERAGE scanner phase (research workflow,
        # not in production). Auto-skip it so phase advances directly from
        # RESOURCES to SPEC_REVIEW.
        session.state.conversation.dev_coverage_complete = True

        # V3 also doesn't implement the ANALYSIS solution-options gate. Stub
        # solution.shape so next_phase() can advance from POINT_B straight to
        # RESOURCES the way it used to before ANALYSIS was introduced.
        if session.state.solution.shape is None:
            session.state.solution.shape = "research mode (v3 mom-test)"
            session.state.solution.shape_description = (
                "v3 momtest workflow — фаза ANALYSIS отключена для research-цикла."
            )
            session.state.solution.needs_ai = True
            session.state.solution.needs_chat_ui = True
            session.state.solution.needs_external_integrations = True

        prev_phase = session.state.conversation.phase
        new_phase = session.state.next_phase()
        session.state.conversation.phase = new_phase
        phase_changed = new_phase != prev_phase

        # First post-screening turn → emit transition + checkpoint
        if session.state.conversation.turn_count == SCREENING_TURNS:
            text = JTBD_TRANSITION + "\n" + self._checkpoint_text(session, new_phase)
            session.append("assistant", text)
            session.state.conversation.last_summary_turn = session.state.conversation.turn_count
            return text

        if new_phase == Phase.DONE and prev_phase != Phase.DONE:
            return self._emit_specs(session)

        turns_since_summary = (
            session.state.conversation.turn_count - session.state.conversation.last_summary_turn
        )
        should_checkpoint = phase_changed or (
            turns_since_summary >= CHECKPOINT_EVERY and not session.state.conversation.confirmed_summary
        )
        if should_checkpoint and self._has_any_facts(session.state):
            text = self._checkpoint_text(session, new_phase)
            session.append("assistant", text)
            session.state.conversation.last_summary_turn = session.state.conversation.turn_count
            return text

        text = self._ask_next(session)
        session.append("assistant", text)
        return text

    # ---- internals ----

    def _mom_test_probe(self, session: WorkflowSession) -> str:
        recent = session.transcript[-6:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        resp = self.llm.complete(
            system=MOMTEST_SYSTEM,
            messages=messages,
            max_tokens=200,
            temperature=0.5,
        )
        return resp.text or "Расскажите чуть подробнее последний случай, когда это болело."

    def _extract(self, session: WorkflowSession) -> None:
        recent = session.transcript[-8:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        resp = self.llm.complete(
            system=_extractor_system(),
            messages=messages,
            tools=[EXTRACTOR_TOOL],
            max_tokens=600,
            temperature=0.0,
        )
        if resp.tool_use and resp.tool_use.get("name") == "update_state":
            _merge_patch(session.state, resp.tool_use.get("input") or {})

    def _ask_next(self, session: WorkflowSession) -> str:
        recent = session.transcript[-8:]
        messages = [{"role": m["role"], "content": m["content"]} for m in recent]
        resp = self.llm.complete(
            system=_asker_system_v3(session.state),
            messages=messages,
            max_tokens=400,
            temperature=0.6,
        )
        return resp.text or "Расскажите чуть больше, пожалуйста."

    def _checkpoint_text(self, session: WorkflowSession, phase: Phase) -> str:
        return SUMMARY_TEMPLATE.render(state=session.state, phase=phase, lang=_LANG)

    def _estimate_duration(self, state_json: str) -> str:
        try:
            resp = self.llm.complete(
                system=(
                    "Ты — solutions architect. На вход — JSON с собранной информацией "
                    "о бизнес-проекте. Дай грубую вилку срока реализации одной "
                    "короткой фразой без пояснений и без markdown. "
                    "Примеры: '2-3 недели', 'около недели', '1-2 месяца'. "
                    "Только фраза, ничего больше."
                ),
                messages=[{"role": "user", "content": state_json}],
                max_tokens=30,
                temperature=0.2,
            )
            txt = (resp.text or "").strip().strip(".").strip("«»\"'")
            return txt or "уточним по ходу"
        except Exception:
            return "уточним по ходу"

    def _emit_specs(self, session: WorkflowSession) -> str:
        state_json = json.dumps(session.state.model_dump(mode="json"), ensure_ascii=False, indent=2)
        biz = self.llm.complete(
            system=BUSINESS_SPEC_PROMPT[_LANG],
            messages=[{"role": "user", "content": state_json}],
            max_tokens=1500,
            temperature=0.3,
        ).text
        dev = self.llm.complete(
            system=DEV_SPEC_PROMPT[_LANG],
            messages=[{"role": "user", "content": state_json}],
            max_tokens=2500,
            temperature=0.2,
        ).text
        session.business_spec = biz
        session.dev_spec = dev
        session.state.conversation.phase = Phase.DONE
        if session.state.conversation.ended_at is None:
            session.state.conversation.ended_at = datetime.now(timezone.utc)

        # Короткий ack — спеки в чат НЕ выводим (только в Firestore/Sheets/Drive
        # для нашей работы). Владельцу — спасибо + оценка времени + CTA на оплату.
        estimate = self._estimate_duration(state_json)
        session.state.conversation.estimate_text = estimate
        combined = (
            "Спасибо, ваш проект принят и сохранён.\n\n"
            f"Примерное время реализации — {estimate}.\n\n"
            "Желаете перейти в чат для оплаты и начала работы?"
        )
        session.append("assistant", combined)
        return combined

    @staticmethod
    def _has_any_facts(state: WorkflowState) -> bool:
        return state.coverage_a() > 0 or state.coverage_b() > 0 or state.coverage_resources() > 0
