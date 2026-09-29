"""Deterministic scripted survey — replaces LLM-generated first turn.

Why: see memory/feedback_deterministic_first_turn.md. The first 6 turns are a
fixed sequence collecting Profile fields. Two of them are free-text (name and
email); the rest are multi-choice (digit + optional comment).

Web flow uses /intake form which submits all 6 fields at once; Telegram uses
this sequential per-turn flow.
"""
from __future__ import annotations

import re
from typing import Literal

# 6 fixed questions matching app.state.Profile fields, in TG order:
#   0 name (free-text)
#   1 gender (digit)
#   2 age_range (digit)
#   3 email (free-text + regex)
#   4 sector (digit)
#   5 time_eater (digit)
#
# Internal storage keys (field, type, options[0] canonical values) are
# byte-identical between RU and EN — only user-visible text/labels translate.

_QUESTIONS_BY_LANG: dict[Literal["ru", "en"], list[dict]] = {
    "ru": [
        {
            "field": "name",
            "type": "free_text",
            "question": "Как могу к вам обращаться?",
        },
        {
            "field": "gender",
            "type": "choice",
            "question": "Какой у вас пол?",
            "options": [
                ("female",        "женский"),
                ("male",          "мужской"),
                ("not_specified", "пропустить"),
            ],
        },
        {
            "field": "age_range",
            "type": "choice",
            "question": "Сколько вам лет?",
            "options": [
                ("<25",   "до 25"),
                ("25-35", "25–35"),
                ("35-45", "35–45"),
                ("45-55", "45–55"),
                ("55+",   "55 и старше"),
            ],
        },
        {
            "field": "email",
            "type": "email",
            "question": (
                "Оставьте email — и через 5 минут у вас будет готов план."
            ),
        },
        {
            "field": "sector",
            "type": "choice",
            "question": "Чем ваш бизнес занимается?",
            "options": [
                ("services",         "услуги (мастер, репетитор, консультант)"),
                ("goods",            "товары / магазин"),
                ("online_education", "онлайн-курсы / инфопродукт"),
                ("production",       "производство / ремесло"),
                ("b2b",              "услуги для других бизнесов (b2b)"),
                ("other",            "другое"),
            ],
        },
        {
            "field": "time_eater",
            "type": "choice",
            "question": "Что у вас сейчас съедает больше всего времени?",
            "options": [
                ("client_comms", "общение с клиентами"),
                ("sales",        "продажи и переговоры"),
                ("production",   "сама работа / производство"),
                ("admin",        "документы, отчёты, рутина"),
                ("marketing",    "маркетинг и привлечение"),
                ("other",        "другое"),
            ],
        },
    ],
    "en": [
        {
            "field": "name",
            "type": "free_text",
            "question": "What name should I use for you?",
        },
        {
            "field": "gender",
            "type": "choice",
            "question": "Your gender?",
            "options": [
                ("female",        "female"),
                ("male",          "male"),
                ("not_specified", "skip"),
            ],
        },
        {
            "field": "age_range",
            "type": "choice",
            "question": "Your age?",
            "options": [
                ("<25",   "under 25"),
                ("25-35", "25–35"),
                ("35-45", "35–45"),
                ("45-55", "45–55"),
                ("55+",   "55 and older"),
            ],
        },
        {
            "field": "email",
            "type": "email",
            "question": (
                "Leave your email — your plan will be ready in 5 minutes."
            ),
        },
        {
            "field": "sector",
            "type": "choice",
            "question": "What does your business do?",
            "options": [
                ("services",         "services"),
                ("goods",            "products / store"),
                ("online_education", "online courses"),
                ("production",       "manufacturing"),
                ("b2b",              "b2b"),
                ("other",            "other"),
            ],
        },
        {
            "field": "time_eater",
            "type": "choice",
            "question": "What eats up the most of your time right now?",
            "options": [
                ("client_comms", "customer chats"),
                ("sales",        "sales"),
                ("production",   "production"),
                ("admin",        "paperwork & routine"),
                ("marketing",    "marketing"),
                ("other",        "other"),
            ],
        },
    ],
}


class _BilingualQuestions:
    """Supports both dict-style ``[lang]`` and legacy list-style ``[int]`` access.

    Legacy callers (agent.py) use ``SCREENING_QUESTIONS[step]`` and
    ``len(SCREENING_QUESTIONS)``; new callers use ``SCREENING_QUESTIONS[lang][step]``.
    Both forms resolve to the RU list unless a lang key is given.
    """

    def __getitem__(self, key):
        if isinstance(key, str):
            return _QUESTIONS_BY_LANG[key]
        # integer index — legacy callers default to RU
        return _QUESTIONS_BY_LANG["ru"][key]

    def __len__(self) -> int:
        return len(_QUESTIONS_BY_LANG["ru"])

    def __iter__(self):
        return iter(_QUESTIONS_BY_LANG["ru"])


SCREENING_QUESTIONS = _BilingualQuestions()


_INTRO_BY_LANG: dict[str, str] = {
    "ru": (
        "Привет! Прежде чем мы начнём — 6 коротких вопросов. "
        "Отвечайте цифрой (для вариантов) или текстом; если хотите — "
        "допишите комментарий в том же сообщении.\n\n"
    ),
    "en": (
        "Hi! Before we begin — 6 quick questions. "
        "Reply with a digit (for choices) or text; if you want, "
        "add a comment in the same message."
    ),
}


class _BilingualStr(str):
    """A ``str`` subclass that also supports ``[lang]`` subscript access.

    Legacy callers use ``SCREENING_INTRO`` as a bare string (defaults to RU);
    new callers use ``SCREENING_INTRO["en"]``.
    """

    def __new__(cls, ru_text: str, by_lang: dict[str, str]):
        obj = super().__new__(cls, ru_text)
        obj._by_lang = by_lang
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._by_lang[key]
        return super().__getitem__(key)


SCREENING_INTRO = _BilingualStr(_INTRO_BY_LANG["ru"], _INTRO_BY_LANG)


_TRANSITION_BY_LANG: dict[str, str] = {
    "ru": (
        "Спасибо! Теперь расскажите своими словами: что в вашем бизнесе сейчас "
        "забирает больше всего времени или денег и хотелось бы это поменять?"
    ),
    "en": (
        "Thanks! Now tell me in your own words: what in your business is eating up "
        "the most time or money right now that you'd like to change?"
    ),
}

TRANSITION_TO_DISCOVERY = _BilingualStr(_TRANSITION_BY_LANG["ru"], _TRANSITION_BY_LANG)


_LEADING_DIGIT = re.compile(r"^\s*(\d+)\b\s*[.,)\-—:]?\s*(.*)$", re.DOTALL)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def is_valid_email(text: str) -> bool:
    return bool(_EMAIL_RE.match(text.strip()))


def format_question(step: int, lang: str = "ru") -> str:
    """Format question with numbered options (for choice-type) or plain (for text)."""
    q = _QUESTIONS_BY_LANG[lang][step]
    if q["type"] == "choice":
        lines = [q["question"]]
        for i, (_value, label) in enumerate(q["options"], start=1):
            lines.append(f"{i}. {label}")
        return "\n".join(lines)
    return q["question"]


def parse_input(text: str, step: int, lang: str = "ru") -> tuple[str | None, str | None]:
    """Parse user's reply to screening question at `step`.

    Returns (canonical_value | None, comment | None).
    Behaviour by question type:

    - **choice**: leading digit 1..len(options) → option value; rest of message
      after digit is comment. Out-of-range or no-digit input → (None, raw text
      as comment).
    - **free_text** (name): whole input is the value; no comment.
    - **email**: whole input goes through regex check. If valid → (email, None);
      if not valid → (None, raw text) to signal re-ask. Caller is responsible
      for re-prompting.
    """
    if not text or not text.strip():
        return None, None
    q = _QUESTIONS_BY_LANG[lang][step]
    qtype = q.get("type", "choice")

    if qtype == "free_text":
        return text.strip(), None

    if qtype == "email":
        candidate = text.strip()
        if is_valid_email(candidate):
            return candidate, None
        return None, candidate  # signals invalid email; caller re-asks

    # choice type
    options = q["options"]
    m = _LEADING_DIGIT.match(text)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(options):
            value = options[idx - 1][0]
            comment = (m.group(2) or "").strip() or None
            return value, comment
    return None, text.strip()


def is_done(step: int) -> bool:
    return step >= len(_QUESTIONS_BY_LANG["ru"])


def field_at(step: int) -> str:
    """Convenience for callers (agent.py meta) that need the field name at this step."""
    return _QUESTIONS_BY_LANG["ru"][step]["field"]
