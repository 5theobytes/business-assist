"""Abstract workflow interface."""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..llm import LLM
from ..state import WorkflowState


@dataclass
class WorkflowSession:
    id: str
    workflow_name: str
    state: WorkflowState = field(default_factory=WorkflowState)
    transcript: list[dict] = field(default_factory=list)  # [{"role": ..., "content": ...}]
    business_spec: str | None = None
    dev_spec: str | None = None
    clarity_doc: str | None = None  # Output A - client clarity document (v4)
    rule_violations: list[dict] = field(default_factory=list)  # for debug / metrics

    def append(self, role: str, content: str, *, meta: dict | None = None) -> None:
        """Add a transcript entry. Optional `meta` lands as a third dict key
        alongside role/content — used to log internal asker state for debugging
        (target_field, phase, internal_rationale) so we can surface it in the
        Sheets export.
        """
        # Run hard guardrails on assistant output (no-op on user messages).
        if role == "assistant":
            from .postprocess import check, sanitize
            lang = self.state.conversation.language
            for v in check(content, lang=lang):
                self.rule_violations.append(
                    {"turn": len(self.transcript), "rule": v.rule,
                     "match": v.match, "severity": v.severity}
                )
            content = sanitize(content, lang=lang)
        entry: dict = {"role": role, "content": content}
        if meta is not None:
            entry["meta"] = meta
        if role == "assistant":
            # Store state snapshot for undo support — state as of this question.
            # By the time append is called, extractors have already run,
            # so this captures state after processing the user's previous answer.
            entry.setdefault("meta", {})["_state_snapshot"] = self.state.model_dump(mode="json")
        self.transcript.append(entry)


class Workflow(ABC):
    name: str = "base"

    def __init__(self, llm: LLM):
        self.llm = llm

    def new_session(self) -> WorkflowSession:
        return WorkflowSession(id=uuid.uuid4().hex, workflow_name=self.name)

    @abstractmethod
    def start(self, session: WorkflowSession) -> str:
        """Return the opening assistant message (and append it to transcript)."""

    @abstractmethod
    def respond(self, session: WorkflowSession, user_message: str) -> str:
        """Process the user's message and return the assistant's reply.

        Must mutate `session.state.conversation.turn_count` and append both messages
        to `session.transcript`.
        """

    def is_done(self, session: WorkflowSession) -> bool:
        return session.state.conversation.phase.value == "done"
