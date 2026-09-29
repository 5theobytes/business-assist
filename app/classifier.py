"""Split each user message into `answer` and `comment` parts.

Strict-JSON tool-use call: the model MUST return both fields as exact verbatim
substrings of the user message. Both spans are persisted to Firestore for
analytics; only the answer part is fed forward into the workflow extractor.
"""
from __future__ import annotations

from .llm import LLM

CLASSIFIER_TOOL = {
    "name": "split_user_message",
    "description": (
        "Раздели последнее сообщение пользователя на две дословные цитаты: "
        "answer_text — содержательный ответ на последний вопрос ассистента "
        "(может быть null если ответа нет вовсе); comment_text — мета-реплика, "
        "встречный вопрос, эмоция, оффтоп (может быть null). Каждая цитата — "
        "точный кусок исходного текста, без перефразирования. Если всё "
        "сообщение целиком — ответ, comment_text=null. Если всё — комментарий, "
        "answer_text=null."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "answer_text": {"type": ["string", "null"]},
            "comment_text": {"type": ["string", "null"]},
            "rationale": {"type": "string"},
        },
        "required": ["answer_text", "comment_text", "rationale"],
        "additionalProperties": False,
    },
}

SYSTEM = (
    "Ты — SPLITTER. Прочитай последний вопрос ассистента и последнее сообщение "
    "пользователя. Вызови инструмент split_user_message ровно один раз. "
    "Цитаты — подстроки исходного сообщения, ничего не перефразируй. "
    "Никакого свободного текста."
)


def split(llm: LLM, *, last_assistant: str, last_user: str) -> dict:
    messages = [
        {"role": "user", "content": f"ВОПРОС АССИСТЕНТА:\n{last_assistant or '(пусто)'}"},
        {"role": "assistant", "content": "Принято, жду сообщение пользователя."},
        {"role": "user", "content": f"СООБЩЕНИЕ ПОЛЬЗОВАТЕЛЯ:\n{last_user}"},
    ]
    resp = llm.complete(
        system=SYSTEM,
        messages=messages,
        tools=[CLASSIFIER_TOOL],
        max_tokens=400,
        temperature=0.0,
    )
    if resp.tool_use and resp.tool_use.get("name") == "split_user_message":
        data = resp.tool_use.get("input") or {}
        ans = data.get("answer_text")
        com = data.get("comment_text")
        if isinstance(ans, str) and not ans.strip():
            ans = None
        if isinstance(com, str) and not com.strip():
            com = None
        return {
            "answer_text": ans,
            "comment_text": com,
            "rationale": (data.get("rationale") or "")[:300],
        }
    return {
        "answer_text": last_user,
        "comment_text": None,
        "rationale": "fallback: splitter did not return tool_use",
    }
