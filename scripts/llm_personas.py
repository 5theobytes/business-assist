"""LLM-driven persona + QA-reviewer for live_load_test sessions.

Replaces the regex-based ``Persona.respond`` (in app/eval/personas.py) with an
LLM persona that actually reads the bot's last question and answers it in
character. A second LLM pass ("QA reviewer") checks each draft and asks for a
regeneration if the persona drifted off-question, switched language, broke
character, or repeated itself verbatim.

The deterministic personas in app/eval/personas.py support repeatable offline
checks. This module generates more varied synthetic replies for a user-selected
staging API, where a real model may ask questions that scripted personas do not
anticipate.

LLM backend: routes through ``app.llm.get_default_llm()`` and the operator's
configured provider credentials. It does not bundle or select credentials.

Public surface:
    - LLMPersonaSession (the per-session stateful responder)
    - CHARACTER_BRIEFS (6 archetype briefs, in Russian)
    - PERSONA_CHARACTER_MAP (persona name -> archetype key)
    - archetype_for(name) -> archetype key
    - build_business_brief(persona) -> str
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.eval.personas import Persona
from app.llm import LLM


PERSONA_MAX_TOKENS = 300
QA_MAX_TOKENS = 200

MAX_QA_ATTEMPTS = 3  # 1 initial + 2 retries


# ---------------------------------------------------------------------------
# Character archetypes (Russian briefs)
# ---------------------------------------------------------------------------

CHARACTER_BRIEFS: dict[str, str] = {
    "confident_techie": (
        "Ты — уверенный технарь-владелец. Тебе 32 года, ты прекрасно знаком с "
        "ChatGPT, Notion, CRM-системами, ставил себе автоматизации в Make.com. "
        "Говоришь по делу, короткими предложениями, оперируешь конкретными "
        "цифрами (часы в день, рубли в месяц, количество клиентов). Не боишься "
        "технических терминов: знаешь, что такое API-ключ, webhook, токены. "
        "Если бот спрашивает что-то с вариантами — выбираешь быстро и без "
        "колебаний. Ценишь время собеседника. Если бот мнётся или повторяется "
        "— можешь поторопить: «давайте к делу»."
    ),
    "vague": (
        "Ты — расплывчатая владелица малого бизнеса, тебе 41 год. Тебе тяжело "
        "сформулировать, что именно болит — описываешь общими словами «всё "
        "сложно», «не успеваю», «как-то путаюсь». Когда бот переспрашивает "
        "конкретику — отвечаешь сначала пространно, потом постепенно "
        "уточняешь. Не оперируешь цифрами без подсказки («ну, наверное, "
        "часов 5 в неделю? может больше»). Tech-savvy на уровне «умею "
        "WhatsApp и Excel». Иногда задаёшь встречные вопросы вроде «а это "
        "вообще можно автоматизировать?». Используешь много вводных слов: "
        "«ну», «как бы», «вот честно»."
    ),
    "skeptic": (
        "Ты — скептичный владелец, 47 лет, тебя в жизни уже разводили подрядчики "
        "на «автоматизации, которые не работают». Первые 2-3 хода ты насторожен: "
        "уточняешь, кто такие, сколько стоит, какие гарантии. На вопросы "
        "отвечаешь, но скупо и с проверкой («а зачем вам это знать?», "
        "«это мне даст что?»). Не выдаёшь цифры пока не поймёшь, что тебя не "
        "пытаются развести. Tech-savvy mid: разбираешься в основных сервисах, "
        "но к новым относишься с подозрением. Постепенно теплеешь, если бот "
        "говорит конкретно и не давит."
    ),
    "rusher": (
        "Ты — спешащий предприниматель, 36 лет. У тебя 5 минут на этот разговор. "
        "Хочешь сразу узнать «сколько стоит и когда можно стартовать», "
        "discovery тебя раздражает. На вопросы отвечаешь односложно, иногда с "
        "ноткой раздражения: «короче, надо CRM», «давайте без воды». Если бот "
        "задаёт открытый вопрос — даёшь минимальный ответ. Tech-savvy "
        "confident: знаешь основные термины. Готов платить за результат, не "
        "готов платить временем за обсуждение."
    ),
    "technophobe": (
        "Ты — технофоб, владелица бизнеса, 53 года. Тебе тяжело с "
        "технологиями: путаешь приложение и сайт, не понимаешь что такое "
        "API, CRM, бот, интеграция. Когда бот использует технический термин — "
        "честно говоришь «не знаю что это» / «не пользовалась». На простые "
        "вопросы про бизнес отвечаешь нормально, по-человечески. Иногда "
        "переспрашиваешь: «а это что значит?». Цифры по бизнесу знаешь "
        "(сколько клиентов, сколько примерно в месяц), но цифры по бюджету "
        "техноинструментов — нет. Любишь, когда говорят простыми словами."
    ),
    "rambler": (
        "Ты — болтун-владелец, 44 года, обожаешь поговорить. На любой вопрос "
        "даёшь развёрнутый ответ с контекстом, отступлениями про семью, детей, "
        "район, прошлый опыт. Можешь начать про дедлайны, а закончить про "
        "племянника, который «как раз учится на программиста». Ответы по "
        "3-5 предложений. Tech-savvy basic: пользуешься тем, чем пользуются "
        "все, вглубь не лезешь. На вопрос о цифрах сначала рассказываешь "
        "историю, потом цифру. Если бот переспрашивает — не обижаешься, "
        "уточняешь."
    ),
}


# Mapping persona name -> archetype key. Each archetype is paired with a
# distinct business case, so --all-cases yields 8 distinct sessions.
PERSONA_CHARACTER_MAP: dict[str, str] = {
    "Дмитрий — онлайн-курсы": "confident_techie",
    "Анна — студия маникюра": "vague",
    "Игорь — сантехник": "skeptic",
    "Алина — SMM-агентство": "rusher",
    "Елена — handmade на маркетплейсах": "technophobe",
    "Марина — кофейня": "rambler",
    "Сергей — репетитор английского": "confident_techie",
    "Павел — налоговый консультант": "confident_techie",
}


def archetype_for(persona_name: str) -> str:
    """Best-effort lookup of archetype for a Persona name. Falls back to 'vague'."""
    if persona_name in PERSONA_CHARACTER_MAP:
        return PERSONA_CHARACTER_MAP[persona_name]
    for key in PERSONA_CHARACTER_MAP:
        if key.split(" — ")[0].lower() in persona_name.lower():
            return PERSONA_CHARACTER_MAP[key]
    return "vague"


# ---------------------------------------------------------------------------
# Business brief (built from existing Persona.intro_message + GroundTruth)
# ---------------------------------------------------------------------------

_VALUE_LABEL = {
    "time": "сэкономить время",
    "money": "перестать терять деньги/заявки",
    "sanity": "снять головную боль",
    "customer_experience": "сделать клиентам удобнее",
}

_TECH_LABEL = {
    "none": "совсем не разбираешься в технике",
    "basic": "базовый уровень — пользуешься Excel/WhatsApp, но не настраивал ничего сам",
    "confident": "уверенный пользователь — знаешь основные сервисы, ставил себе автоматизации",
}


def build_business_brief(persona: Persona) -> str:
    """Build a Russian business brief for the persona LLM from existing data."""
    gt = persona.ground_truth
    out_of_scope = "; ".join(gt.out_of_scope) if gt.out_of_scope else "ничего особенно не исключаешь"
    has_key = (
        f"да, у тебя есть подписка на {gt.llm_provider} (есть API-ключ)"
        if gt.has_llm_key
        else "у тебя нет подписки на ChatGPT/Claude/других LLM, ключа нет"
    )
    return (
        f"Описание бизнеса (твоими словами): {persona.intro_message}\n\n"
        f"Ключевые факты, которые ты держишь в голове:\n"
        f"- Тип бизнеса: {gt.business_type}\n"
        f"- Каналы, через которые приходят клиенты: {', '.join(gt.channels)}\n"
        f"- Чем сейчас пользуешься для работы: {', '.join(gt.tools_in_use)}\n"
        f"- Что болит / отнимает время: {'; '.join(gt.pain_points)}\n"
        f"- Что для тебя сейчас главное: {_VALUE_LABEL.get(gt.primary_value, gt.primary_value)}\n"
        f"- Как должно стать после внедрения (одной фразой): {gt.desired_outcome}\n"
        f"- Как поймёшь, что получилось (метрика): {gt.success_metric}\n"
        f"- Что точно НЕ нужно делать: {out_of_scope}\n"
        f"- Бюджет: готов(а) тратить около {gt.monthly_budget_rub} ₽/мес\n"
        f"- LLM-подписка: {has_key}\n"
        f"- Тех-уровень: {_TECH_LABEL.get(gt.tech_savviness, gt.tech_savviness)}\n"
        f"- Кто будет поддерживать после запуска: {gt.maintainer}\n"
    )


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

PERSONA_SYSTEM_TEMPLATE = """Ты — {name}, владелец малого бизнеса. {character_brief}

ТВОЙ БИЗНЕС:
{business_brief}

ПРАВИЛА:
- Отвечай как живой человек в чате с консультантом — коротко, по делу, в 1-3 предложения.
- Не выдавай всю информацию сразу. Отвечай ТОЛЬКО на тот вопрос, который задал бот.
- Сохраняй характер на протяжении всего диалога.
- Все ответы СТРОГО НА РУССКОМ ЯЗЫКЕ.
- Если бот задаёт вопрос с вариантами цифрами — выбирай уместный для своего характера и пиши вариант словами или цифрой+пояснением.
- Если бот спрашивает что-то, чего твой персонаж бы не знал — так и скажи "не знаю" / "не пользовался"."""


QA_SYSTEM = """Ты — QA-проверяющий для тестовой персоны малого бизнеса. Тебе дают:
1. Вопрос бота (последний)
2. Проект ответа персоны
3. Краткий характер персоны
4. Историю диалога (последние 4 хода)

Оцени проект ответа по критериям:
1. Соответствует ли ответ заданному вопросу — не отвечает ли на другой вопрос (например, на бюджетный вопрос когда бот спросил про техподдержку)?
2. Строго ли на русском языке?
3. Соответствует ли характеру персоны?
4. Не повторяет ли дословно предыдущий ответ персоны?

Верни только JSON:
{"ok": true} — если всё в порядке.
{"ok": false, "reason": "...", "correction_note": "что должна сделать персона при перегенерации, 1 предложение"} — если плохо."""


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


@dataclass
class _Turn:
    role: str  # "bot" or "me"
    content: str


@dataclass
class LLMPersonaSession:
    """Stateful LLM-driven persona for one live test session.

    Persona generation IS an LLM call (one per bot turn, plus one for the QA
    reviewer = 2/turn nominal). Routes through ``app.llm.get_default_llm()``
    so persona calls use the operator-configured backend. Each generated turn
    and QA review can incur provider usage and charges.
    """

    name: str
    character_brief: str
    business_brief: str
    llm: LLM
    intro_message: str = ""

    # Mutable state
    _history: list[_Turn] = field(default_factory=list)
    _qa_notes: list[str] = field(default_factory=list)
    _last_my_reply: str | None = None

    def __post_init__(self) -> None:
        # Seed history with the intro as our first turn (it's already been sent).
        if self.intro_message:
            self._history.append(_Turn(role="me", content=self.intro_message))
            self._last_my_reply = self.intro_message

    # ------------------------------------------------------------------ public

    def respond(
        self, bot_message: str, *, run_qa: bool = True,
    ) -> tuple[str, list[dict]]:
        """Generate the next user reply.

        Returns (final_user_reply, qa_log_for_this_turn).

        qa_log entries: {attempt, draft, qa_verdict, regenerated, warning?}
        If `run_qa=False` the persona LLM is called once, no QA reviewer; the
        QA log will have one entry with `qa_verdict = {"ok": true, "skipped": true}`.
        """
        self._history.append(_Turn(role="bot", content=bot_message))

        qa_log: list[dict] = []
        accepted: str | None = None

        max_attempts = MAX_QA_ATTEMPTS if run_qa else 1
        for attempt in range(1, max_attempts + 1):
            draft = self._call_persona(bot_message)
            verdict: dict[str, Any]
            if run_qa:
                verdict = self._call_qa(bot_message, draft)
            else:
                verdict = {"ok": True, "skipped": True}
            entry: dict[str, Any] = {
                "attempt": attempt,
                "draft": draft,
                "qa_verdict": verdict,
                "regenerated": False,
            }
            if verdict.get("ok"):
                accepted = draft
                qa_log.append(entry)
                break
            note = verdict.get("correction_note") or verdict.get("reason") or ""
            if note:
                self._qa_notes.append(note)
            entry["regenerated"] = attempt < max_attempts
            qa_log.append(entry)

        if accepted is None:
            last = qa_log[-1]
            accepted = last["draft"]
            last["warning"] = "qa_exhausted"

        self._history.append(_Turn(role="me", content=accepted))
        self._last_my_reply = accepted
        self._qa_notes = self._qa_notes[-3:]
        return accepted, qa_log

    # ----------------------------------------------------------------- private

    def _persona_system_prompt(self) -> str:
        base = PERSONA_SYSTEM_TEMPLATE.format(
            name=self.name,
            character_brief=self.character_brief,
            business_brief=self.business_brief,
        )
        if self._qa_notes:
            notes = " | ".join(self._qa_notes)
            base += f"\n\nВНИМАНИЕ от QA: {notes}"
        return base

    def _format_history(self, max_turns: int | None = None) -> str:
        turns = self._history if max_turns is None else self._history[-max_turns:]
        lines = []
        for t in turns:
            speaker = "БОТ" if t.role == "bot" else "Я"
            lines.append(f"{speaker}: {t.content}")
        return "\n".join(lines)

    def _call_persona(self, bot_message: str) -> str:
        history_text = self._format_history()
        user_msg = (
            f"История диалога:\n{history_text}\n\n"
            f"Последняя реплика бота — ответь на это (одно сообщение, 1-3 предложения, "
            f"только на русском, в характере):\n«{bot_message}»"
        )
        resp = self.llm.complete(
            system=self._persona_system_prompt(),
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=PERSONA_MAX_TOKENS,
            temperature=0.6,
        )
        text = (resp.text or "").strip()
        if len(text) >= 2 and text[0] in {'"', "«", "'"} and text[-1] in {'"', "»", "'"}:
            text = text[1:-1].strip()
        return text or "хм, не уверен что ответить"

    def _call_qa(self, bot_message: str, draft: str) -> dict:
        history_text = self._format_history(max_turns=8)
        prev_reply = self._last_my_reply or "(пока не было)"
        user_msg = (
            f"ХАРАКТЕР ПЕРСОНЫ: {self.character_brief}\n\n"
            f"ИСТОРИЯ (последние ходы):\n{history_text}\n\n"
            f"ПРЕДЫДУЩИЙ МОЙ ОТВЕТ: {prev_reply}\n\n"
            f"ВОПРОС БОТА: {bot_message}\n\n"
            f"ПРОЕКТ ОТВЕТА ПЕРСОНЫ: {draft}\n\n"
            f"Верни только JSON, как описано в системном промпте."
        )
        resp = self.llm.complete(
            system=QA_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
            max_tokens=QA_MAX_TOKENS,
            temperature=0.0,
        )
        return _parse_qa_json(resp.text or "")


def _parse_qa_json(text: str) -> dict:
    """Best-effort parse of QA verdict JSON. On any failure, default to ok=True."""
    if not text:
        return {"ok": True, "_parse_error": "empty"}
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(t[start: end + 1])
        except Exception:
            pass
    return {"ok": True, "_parse_error": "could not parse", "_raw": text[:200]}
