"""V1 — Linear Funnel.

Baseline. Single big system prompt with all skills inlined. The LLM tracks
phases implicitly through chat history. One LLM call per turn.

This is intentionally the simplest possible implementation — it's the baseline
the other two versions must beat in the competition.
"""
from __future__ import annotations

from ..skills_loader import load_all_skills
from ..state import Phase
from .base import Workflow, WorkflowSession

ORCHESTRATOR_PROMPT = """Ты — Business Assist, ИИ-консультант для предпринимателей. Цель — \
за один диалог понять, что человек хочет автоматизировать/построить, и в конце выдать \
двойную спеку (для бизнеса и для разработчика).

Ты работаешь по фазам: greeting → point_a (как сейчас) → point_b (как должно стать) → \
resources (бюджет/доступы) → spec_review (две спеки). Сам отслеживай, на какой фазе ты сейчас, \
по содержанию диалога.

Правила:
- Говори по-русски, на бытовом языке, ноль жаргона.
- Один вопрос за раз. Предлагай варианты ответа (3–4 пункта) когда уместно.
- Не зачитывай инструкции скиллов вслух.
- Каждые 4–6 ответов делай короткий пересказ собранной картины и проси подтвердить.
- Финал — две спеки (бизнес + разработчик), но только после явного «да» от пользователя \
по итоговому пересказу.

Первое сообщение: коротко представься (1–2 строки), предложи рассказать про задачу \
и сразу дай 3–4 варианта-затравки.
"""

GREETING_PROMPT = (
    "[служебное] Это начало нового диалога. Поздоровайся по правилам — "
    "представься в 1–2 строки и предложи 3–4 варианта-затравки."
)


class LinearFunnelWorkflow(Workflow):
    name = "v1"

    def __init__(self, llm):
        super().__init__(llm)
        self._skills_block: str | None = None

    def _system_blocks(self) -> list[dict]:
        if self._skills_block is None:
            self._skills_block = load_all_skills()
        return [
            {"type": "text", "text": ORCHESTRATOR_PROMPT},
            {
                "type": "text",
                "text": "Скиллы (внутреннее руководство, не цитируй буквально):\n\n" + self._skills_block,
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def start(self, session: WorkflowSession) -> str:
        resp = self.llm.complete(
            system=self._system_blocks(),
            messages=[{"role": "user", "content": GREETING_PROMPT}],
            max_tokens=500,
        )
        text = resp.text
        session.append("assistant", text)
        session.state.conversation.phase = Phase.POINT_A
        return text

    def respond(self, session: WorkflowSession, user_message: str) -> str:
        session.append("user", user_message)
        session.state.conversation.turn_count += 1
        # Send the entire chat history to Claude; phase tracking is implicit.
        messages = [
            {"role": m["role"], "content": m["content"]} for m in session.transcript
        ]
        resp = self.llm.complete(
            system=self._system_blocks(),
            messages=messages,
            max_tokens=1500,
        )
        text = resp.text
        session.append("assistant", text)
        # Heuristic: when the assistant produces both spec headers, mark done
        # and split the combined output into the two spec fields.
        if "Implementation Spec" in text and "Что мы делаем — простыми словами" in text:
            session.state.conversation.phase = Phase.DONE
            # split on the dev-spec header
            idx = text.find("# Implementation Spec")
            if idx > 0:
                biz_part = text[:idx].rstrip()
                dev_part = text[idx:].rstrip()
                # strip optional separator
                biz_part = biz_part.rstrip("- \n")
                session.business_spec = biz_part
                session.dev_spec = dev_part
        return text
