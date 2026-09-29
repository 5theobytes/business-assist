"""Persistence adapters for workflow sessions and classifications.

Firestore provides durable storage; InMemoryStore supports local development
and tests that do not need persistence across process restarts.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any

from .state import WorkflowState
from .workflows import WorkflowSession


class SessionStore(ABC):
    @abstractmethod
    def save(self, session: WorkflowSession, *, telegram_chat_id: int | None = None) -> None: ...

    @abstractmethod
    def load(self, session_id: str) -> WorkflowSession | None: ...

    @abstractmethod
    def find_by_telegram_chat(self, chat_id: int) -> WorkflowSession | None: ...

    @abstractmethod
    def append_classification(
        self, session_id: str, *, turn: int, kind: str, text: str, rationale: str
    ) -> None: ...


def _session_to_dict(session: WorkflowSession, telegram_chat_id: int | None) -> dict[str, Any]:
    return {
        "workflow_name": session.workflow_name,
        "state": session.state.model_dump(mode="json"),
        "transcript": list(session.transcript),
        "business_spec": session.business_spec,
        "dev_spec": session.dev_spec,
        "rule_violations": list(session.rule_violations),
        "telegram_chat_id": telegram_chat_id,
    }


def _dict_to_session(session_id: str, data: dict[str, Any]) -> WorkflowSession:
    return WorkflowSession(
        id=session_id,
        workflow_name=data.get("workflow_name", "v2"),
        state=WorkflowState.model_validate(data.get("state") or {}),
        transcript=list(data.get("transcript") or []),
        business_spec=data.get("business_spec"),
        dev_spec=data.get("dev_spec"),
        rule_violations=list(data.get("rule_violations") or []),
    )


class FirestoreStore(SessionStore):
    def __init__(self, collection: str = "sessions", database: str | None = None):
        from google.cloud import firestore

        self.collection = collection
        self.database = database or os.environ.get("FIRESTORE_DATABASE") or "(default)"
        self.client = firestore.Client(database=self.database)
        self._firestore = firestore

    def save(self, session, *, telegram_chat_id=None):
        doc = self.client.collection(self.collection).document(session.id)
        payload = _session_to_dict(session, telegram_chat_id)
        payload["updated_at"] = self._firestore.SERVER_TIMESTAMP
        doc.set(payload, merge=True)

    def load(self, session_id):
        snap = self.client.collection(self.collection).document(session_id).get()
        if not snap.exists:
            return None
        return _dict_to_session(session_id, snap.to_dict() or {})

    def find_by_telegram_chat(self, chat_id):
        # Filter-only queries avoid requiring a composite Firestore index.
        # Sort the small per-chat result set in Python.
        q = self.client.collection(self.collection).where(
            filter=self._firestore.FieldFilter("telegram_chat_id", "==", chat_id),
        )
        candidates: list[tuple[Any, str, dict[str, Any]]] = []
        for snap in q.stream():
            data = snap.to_dict() or {}
            candidates.append((data.get("updated_at"), snap.id, data))
        if not candidates:
            return None
        with_ts = [c for c in candidates if c[0] is not None]
        if with_ts:
            with_ts.sort(key=lambda x: x[0], reverse=True)
            _, sid, data = with_ts[0]
        else:
            # All docs missing updated_at — pick deterministically by id so two
            # races on the same chat resolve the same way.
            candidates.sort(key=lambda x: x[1])
            _, sid, data = candidates[0]
        return _dict_to_session(sid, data)

    def append_classification(self, session_id, *, turn, kind, text, rationale):
        doc = self.client.collection(self.collection).document(session_id)
        doc.set(
            {
                "classifications": self._firestore.ArrayUnion(
                    [{"turn": turn, "type": kind, "text": text, "rationale": rationale}]
                ),
                "updated_at": self._firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )


class InMemoryStore(SessionStore):
    """In-memory session store for local dev/testing (SESSION_STORE=memory)."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._classifications: dict[str, list] = {}

    def save(self, session, *, telegram_chat_id=None):
        self._sessions[session.id] = _session_to_dict(session, telegram_chat_id)

    def load(self, session_id):
        data = self._sessions.get(session_id)
        if data is None:
            return None
        return _dict_to_session(session_id, data)

    def find_by_telegram_chat(self, chat_id):
        for sid, data in self._sessions.items():
            if data.get("telegram_chat_id") == chat_id:
                return _dict_to_session(sid, data)
        return None

    def append_classification(self, session_id, *, turn, kind, text, rationale):
        self._classifications.setdefault(session_id, []).append(
            {"turn": turn, "type": kind, "text": text, "rationale": rationale}
        )


def get_default_store() -> SessionStore:
    if (os.environ.get("SESSION_STORE") or "").strip().lower() == "memory":
        return InMemoryStore()
    return FirestoreStore(
        collection=os.environ.get("FIRESTORE_COLLECTION", "sessions"),
        database=os.environ.get("FIRESTORE_DATABASE") or None,
    )
