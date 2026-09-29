"""Tests for app/session_store.py — Firestore-backed persistence.

These hit real Firestore (collection `sessions_test`). Skip if creds missing.
"""
from __future__ import annotations

import os
import uuid

import pytest

import app.bootstrap  # noqa: F401  side-effect: materialise creds before google import


def _has_firestore_creds() -> bool:
    return bool(
        os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        or os.environ.get("GCP_PROJECT_ID")
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_firestore,
    pytest.mark.skipif(
        not _has_firestore_creds(),
        reason="Firestore credentials missing",
    ),
]


TEST_COLLECTION = "sessions_test"


@pytest.fixture
def store():
    from app.session_store import FirestoreStore
    return FirestoreStore(collection=TEST_COLLECTION)


@pytest.fixture
def created_ids():
    ids: list[str] = []
    yield ids
    # teardown: delete every doc this test created (same DB as the store fixture)
    from google.cloud import firestore
    client = firestore.Client(database=os.environ.get("FIRESTORE_DATABASE") or "(default)")
    for sid in ids:
        client.collection(TEST_COLLECTION).document(sid).delete()


def test_constructor_keeps_collection_name(store):
    assert store.collection == TEST_COLLECTION


def test_save_then_load_roundtrip(store, created_ids):
    from app.workflows import WorkflowSession

    sid = f"test-{uuid.uuid4().hex}"
    session = WorkflowSession(id=sid, workflow_name="v2")
    session.transcript.append({"role": "assistant", "content": "Привет!"})
    session.state.point_a.team_size = 3
    created_ids.append(sid)

    store.save(session)
    loaded = store.load(sid)

    assert loaded is not None
    assert loaded.id == sid
    assert loaded.workflow_name == "v2"
    assert loaded.transcript[0]["content"] == "Привет!"
    assert loaded.state.point_a.team_size == 3


def test_load_returns_none_for_missing(store):
    assert store.load(f"missing-{uuid.uuid4().hex}") is None


def test_find_by_telegram_chat_returns_latest(store, created_ids):
    from app.workflows import WorkflowSession

    chat_id = 9999000 + int(uuid.uuid4().int % 100000)
    sid = f"test-{uuid.uuid4().hex}"
    session = WorkflowSession(id=sid, workflow_name="v2")
    session.transcript.append({"role": "assistant", "content": "Hi"})
    created_ids.append(sid)

    store.save(session, telegram_chat_id=chat_id)
    found = store.find_by_telegram_chat(chat_id)

    assert found is not None
    assert found.id == sid


def test_find_by_telegram_chat_returns_none_when_no_match(store):
    found = store.find_by_telegram_chat(123_456_789_000)
    assert found is None


def test_find_by_telegram_chat_returns_most_recent_of_many(store, created_ids):
    """When the same chat has multiple historical sessions, return the latest.

    Regression guard for the no-composite-index refactor: the query no longer
    uses Firestore's order_by + limit, so latest-selection happens in Python.
    """
    import time
    from app.workflows import WorkflowSession

    chat_id = 9998000 + int(uuid.uuid4().int % 100000)
    sids: list[str] = []
    for i in range(3):
        sid = f"test-{uuid.uuid4().hex}"
        sids.append(sid)
        created_ids.append(sid)
        session = WorkflowSession(id=sid, workflow_name="v2")
        session.transcript.append({"role": "assistant", "content": f"msg {i}"})
        store.save(session, telegram_chat_id=chat_id)
        # Firestore SERVER_TIMESTAMP needs a real wallclock gap to differ.
        time.sleep(1.1)

    found = store.find_by_telegram_chat(chat_id)
    assert found is not None
    assert found.id == sids[-1], f"expected latest sid {sids[-1]}, got {found.id}"


def test_append_classification_accumulates(store, created_ids):
    from app.workflows import WorkflowSession

    sid = f"test-{uuid.uuid4().hex}"
    session = WorkflowSession(id=sid, workflow_name="v2")
    created_ids.append(sid)
    store.save(session)

    store.append_classification(
        sid, turn=1, kind="answer", text="Нас трое.", rationale="ответ на вопрос про команду",
    )
    store.append_classification(
        sid, turn=1, kind="comment", text="А зачем тебе это?", rationale="мета-вопрос",
    )

    snap = store.client.collection(store.collection).document(sid).get()
    classifications = snap.to_dict().get("classifications") or []

    assert len(classifications) == 2
    types = {c["type"] for c in classifications}
    texts = {c["text"] for c in classifications}
    assert types == {"answer", "comment"}
    assert "Нас трое." in texts and "А зачем тебе это?" in texts
