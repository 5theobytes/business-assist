"""Run a (workflow, persona) pair to completion or until max_turns.

Pure orchestration — both the workflow's LLM and the persona's responder are
injected, so this can run with MockLLM (offline/tests) or with real Claude
(production-quality transcripts).
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ..llm import LLM, MockLLM
from ..workflows import Workflow, WorkflowSession, build
from .mock_llm import mock_responder
from .personas import Persona


@dataclass
class SimulationResult:
    workflow_name: str
    persona_name: str
    transcript: list[dict] = field(default_factory=list)
    final_state: dict | None = None
    business_spec: str | None = None
    dev_spec: str | None = None
    completed: bool = False
    turn_count: int = 0
    duration_s: float = 0.0
    metrics: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @property
    def avg_bot_msg_len(self) -> float:
        bot_msgs = [m for m in self.transcript if m["role"] == "assistant"]
        if not bot_msgs:
            return 0.0
        return sum(len(m["content"]) for m in bot_msgs) / len(bot_msgs)


def simulate(
    workflow_name: str,
    persona: Persona,
    *,
    llm: LLM | None = None,
    max_turns: int = 25,
) -> SimulationResult:
    if llm is None:
        llm = MockLLM(responder=mock_responder)
    workflow: Workflow = build(workflow_name, llm=llm)
    session: WorkflowSession = workflow.new_session()

    started = time.time()
    workflow.start(session)

    for turn in range(1, max_turns + 1):
        if workflow.is_done(session):
            break
        bot_message = session.transcript[-1]["content"]
        user_reply = persona.respond(bot_message, turn)
        # Stop if persona signals completion
        workflow.respond(session, user_reply)

    duration = time.time() - started
    return SimulationResult(
        workflow_name=workflow_name,
        persona_name=persona.name,
        transcript=list(session.transcript),
        final_state=session.state.model_dump(mode="json"),
        business_spec=session.business_spec,
        dev_spec=session.dev_spec,
        completed=workflow.is_done(session),
        turn_count=session.state.conversation.turn_count,
        duration_s=duration,
    )
