"""Tests for the undo endpoint and agent.undo() logic."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.state import Phase, WorkflowState
from app.workflows.base import WorkflowSession


# ---- helpers ----

def _make_session(
    *,
    phase: Phase = Phase.POINT_A,
    assistant_messages: list[dict] | None = None,
) -> WorkflowSession:
    """Build a WorkflowSession with a realistic transcript.

    Each entry in `assistant_messages` is a dict with keys:
      content (str), meta (dict | None)
    User messages are auto-inserted between assistant messages.
    """
    session = WorkflowSession(id="test-sid-123", workflow_name="v4")
    session.state = WorkflowState()
    session.state.conversation.phase = phase
    session.state.conversation.language = "ru"

    if assistant_messages is None:
        assistant_messages = [
            {"content": "Привет! Какой у вас бизнес?", "meta": {"phase": "point_a", "kind": "opener", "suggested_options": ["Расскажу про свой бизнес", "У меня проблема"]}},
            {"content": "Сколько у вас сотрудников?", "meta": {"phase": "point_a", "kind": "asker", "suggested_options": ["1-3", "4-10", "10+"]}},
            {"content": "Какой у вас бюджет?", "meta": {"phase": "resources", "kind": "asker", "suggested_options": ["До 10 000", "10 000-50 000"]}},
        ]

    for i, msg in enumerate(assistant_messages):
        if i > 0:
            session.transcript.append({"role": "user", "content": f"answer {i}"})
        session.transcript.append({
            "role": "assistant",
            "content": msg["content"],
            "meta": msg.get("meta"),
        })

    # Set turn_count to match transcript length
    session.state.conversation.turn_count = len(session.transcript)
    return session


# ---- agent.undo() unit tests ----

@pytest.fixture
def mock_agent_deps(monkeypatch):
    """Mock _get_store and _trigger_sheets_sync for agent.undo() tests."""
    from app import agent
    mock_store = MagicMock()
    monkeypatch.setattr(agent, "_get_store", lambda: mock_store)
    monkeypatch.setattr(agent, "_trigger_sheets_sync", lambda: None)
    return mock_store


def test_agent_undo_trims_to_target_step(mock_agent_deps):
    """agent.undo() keeps transcript up to target assistant message."""
    from app import agent

    session = _make_session()
    # 3 assistant messages (steps 1, 2, 3), with user messages between
    # Transcript: [assistant1, user1, assistant2, user2, assistant3]
    assert len(session.transcript) == 5

    # Undo to step 1 → keep only the first assistant message
    result = agent.undo(session, target_step=1)

    # Should only have the first assistant message left
    assert len(session.transcript) == 1
    assert session.transcript[0]["role"] == "assistant"
    assert session.transcript[0]["content"] == "Привет! Какой у вас бизнес?"
    assert result == "Привет! Какой у вас бизнес?"


def test_agent_undo_trims_to_step_2(mock_agent_deps):
    """agent.undo() with target_step=2 keeps first two assistant messages + intervening user."""
    from app import agent

    session = _make_session()
    # Undo to step 2 → keep assistant1, user1, assistant2
    result = agent.undo(session, target_step=2)

    assert len(session.transcript) == 3
    assert session.transcript[-1]["role"] == "assistant"
    assert session.transcript[-1]["content"] == "Сколько у вас сотрудников?"
    assert result == "Сколько у вас сотрудников?"


def test_agent_undo_resets_phase(mock_agent_deps):
    """agent.undo() updates phase from the remaining last assistant meta."""
    from app import agent

    session = _make_session()
    assert session.state.conversation.phase == Phase.POINT_A

    # Undo to step 3 (last assistant) — phase should come from its meta ("resources")
    agent.undo(session, target_step=3)
    assert session.state.conversation.phase == Phase.RESOURCES

    # Now undo to step 1 — phase should come from its meta ("point_a")
    agent.undo(session, target_step=1)
    assert session.state.conversation.phase == Phase.POINT_A


def test_agent_undo_resets_confirmed_summary(mock_agent_deps):
    """agent.undo() resets confirmed_summary so the workflow re-asks if needed."""
    from app import agent

    session = _make_session()
    session.state.conversation.confirmed_summary = True

    agent.undo(session, target_step=2)

    assert session.state.conversation.confirmed_summary is False


def test_agent_undo_resets_turn_count(mock_agent_deps):
    """agent.undo() resets turn_count to match new transcript length."""
    from app import agent

    session = _make_session()
    assert session.state.conversation.turn_count == 5

    agent.undo(session, target_step=1)

    assert session.state.conversation.turn_count == 1


def test_agent_undo_invalid_step_raises(mock_agent_deps):
    """agent.undo() raises ValueError for step > assistant count."""
    from app import agent

    session = _make_session()
    # 3 assistant messages
    with pytest.raises(ValueError, match="out of range"):
        agent.undo(session, target_step=4)


def test_agent_undo_step_zero_raises(mock_agent_deps):
    """agent.undo() raises ValueError for step < 1."""
    from app import agent

    session = _make_session()
    with pytest.raises(ValueError, match="out of range"):
        agent.undo(session, target_step=0)


def test_agent_undo_negative_step_raises(mock_agent_deps):
    """agent.undo() raises ValueError for negative step."""
    from app import agent

    session = _make_session()
    with pytest.raises(ValueError, match="out of range"):
        agent.undo(session, target_step=-1)


def test_agent_undo_handles_arrow_phase(mock_agent_deps):
    """agent.undo() uses the last segment of arrow phase strings (e.g. 'screening→point_a')."""
    from app import agent

    session = _make_session(assistant_messages=[
        {"content": "Как вас зовут?", "meta": {"phase": "screening→point_a", "kind": "screening_transition"}},
        {"content": "Расскажите о бизнесе.", "meta": {"phase": "point_a", "kind": "asker"}},
    ])
    # Undo to step 1 — phase should resolve to "point_a" (after →)
    agent.undo(session, target_step=1)
    assert session.state.conversation.phase == Phase.POINT_A


def test_agent_undo_no_meta_leaves_phase_unchanged(mock_agent_deps):
    """agent.undo() leaves phase unchanged when last message has no meta/phase."""
    from app import agent

    session = _make_session(assistant_messages=[
        {"content": "Hello", "meta": None},
    ])
    # Phase is POINT_A initially (from session creation)
    session.state.conversation.phase = Phase.POINT_A

    agent.undo(session, target_step=1)
    # Phase should stay POINT_A since there's no meta to derive from
    assert session.state.conversation.phase == Phase.POINT_A


# ---- /api/chat/undo endpoint tests ----

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from app.main import app
    return TestClient(app)


def _setup_agent_mocks(monkeypatch, *, session=None, undo_result="Rewound question"):
    """Set up agent.is_configured, agent.get_session, and agent.undo mocks."""
    from app import agent

    monkeypatch.setattr(agent, "is_configured", lambda: True)

    if session is None:
        session = _make_session()

    monkeypatch.setattr(agent, "get_session", lambda sid: session)

    def _fake_undo(sess, target_step):
        # Simulate what undo does: trim transcript, return last content
        assistant_indices = [i for i, m in enumerate(sess.transcript) if m["role"] == "assistant"]
        if target_step < 1 or target_step > len(assistant_indices):
            raise ValueError(f"target_step {target_step} out of range (1..{len(assistant_indices)})")
        cut_idx = assistant_indices[target_step - 1]
        sess.transcript = sess.transcript[:cut_idx + 1]
        return undo_result

    monkeypatch.setattr(agent, "undo", _fake_undo)


def test_undo_endpoint_trims_transcript(client, monkeypatch):
    """POST /api/chat/undo returns the question at target_step and trims."""
    session = _make_session()
    _setup_agent_mocks(monkeypatch, session=session, undo_result="Привет! Какой у вас бизнес?")

    r = client.post("/api/chat/undo", json={
        "session_id": "test-sid-123",
        "target_step": 1,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["reply"] == "Привет! Какой у вас бизнес?"


def test_undo_endpoint_includes_suggested_options(client, monkeypatch):
    """POST /api/chat/undo returns suggested_options from the last meta."""
    session = _make_session()
    _setup_agent_mocks(monkeypatch, session=session, undo_result="Сколько у вас сотрудников?")

    r = client.post("/api/chat/undo", json={
        "session_id": "test-sid-123",
        "target_step": 2,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    # After undo, the last assistant message in transcript should have its meta
    # Our fake undo keeps through step 2, so the last transcript entry has
    # meta with suggested_options from step 2.
    assert body["suggested_options"] == ["1-3", "4-10", "10+"]


def test_undo_endpoint_includes_phase_marker(client, monkeypatch):
    """POST /api/chat/undo returns phase_marker from the last meta."""
    session = _make_session()
    _setup_agent_mocks(monkeypatch, session=session, undo_result="Какой у вас бюджет?")

    r = client.post("/api/chat/undo", json={
        "session_id": "test-sid-123",
        "target_step": 3,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["phase_marker"] == "resources"


def test_undo_endpoint_includes_clarity_score(client, monkeypatch):
    """POST /api/chat/undo returns clarity_score from session state."""
    session = _make_session()
    session.state.solution.clarity_score = {"point_a": 50, "point_b": 30, "resources": 10}
    _setup_agent_mocks(monkeypatch, session=session, undo_result="Question")

    r = client.post("/api/chat/undo", json={
        "session_id": "test-sid-123",
        "target_step": 1,
    })

    assert r.status_code == 200
    body = r.json()
    assert body["clarity_score"] == {"point_a": 50, "point_b": 30, "resources": 10}


def test_undo_endpoint_session_not_found(client, monkeypatch):
    """POST /api/chat/undo returns 404 for unknown session."""
    from app import agent

    monkeypatch.setattr(agent, "is_configured", lambda: True)
    monkeypatch.setattr(agent, "get_session", lambda sid: None)

    r = client.post("/api/chat/undo", json={
        "session_id": "nonexistent",
        "target_step": 1,
    })

    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_undo_endpoint_done_session(client, monkeypatch):
    """POST /api/chat/undo returns 400 for completed session."""
    session = _make_session(phase=Phase.DONE)
    _setup_agent_mocks(monkeypatch, session=session)

    r = client.post("/api/chat/undo", json={
        "session_id": "test-sid-123",
        "target_step": 1,
    })

    assert r.status_code == 400
    assert "completed" in r.json()["detail"].lower()


def test_undo_endpoint_invalid_step(client, monkeypatch):
    """POST /api/chat/undo returns 400 for out-of-range step."""
    from app import agent

    session = _make_session()
    monkeypatch.setattr(agent, "is_configured", lambda: True)
    monkeypatch.setattr(agent, "get_session", lambda sid: session)

    def _raise_undo(sess, target_step):
        raise ValueError(f"target_step {target_step} out of range (1..3)")

    monkeypatch.setattr(agent, "undo", _raise_undo)

    r = client.post("/api/chat/undo", json={
        "session_id": "test-sid-123",
        "target_step": 99,
    })

    assert r.status_code == 400
    assert "out of range" in r.json()["detail"]


def test_undo_endpoint_not_configured(client, monkeypatch):
    """POST /api/chat/undo returns 503 when agent is not configured."""
    from app import agent

    monkeypatch.setattr(agent, "is_configured", lambda: False)

    r = client.post("/api/chat/undo", json={
        "session_id": "any",
        "target_step": 1,
    })

    assert r.status_code == 503


# ---- state snapshot restoration tests ----

def test_agent_undo_restores_state_from_snapshot(mock_agent_deps):
    """agent.undo() restores full state from _state_snapshot in meta."""
    from app import agent

    session = WorkflowSession(id="snap-test", workflow_name="v4")
    session.state.conversation.language = "ru"

    # Step 1: assistant asks about business — state is empty
    step1_state = WorkflowState()
    step1_state.conversation.phase = Phase.POINT_A
    step1_state.conversation.language = "ru"
    step1_state.conversation.turn_count = 1
    session.transcript.append({
        "role": "assistant",
        "content": "Какой у вас бизнес?",
        "meta": {
            "phase": "point_a",
            "kind": "opener",
            "_state_snapshot": step1_state.model_dump(mode="json"),
        },
    })

    # User answers
    session.transcript.append({"role": "user", "content": "У меня SaaS-стартап"})

    # Step 2: assistant asks about team — state has point_a populated
    step2_state = WorkflowState()
    step2_state.conversation.phase = Phase.POINT_A
    step2_state.conversation.language = "ru"
    step2_state.conversation.turn_count = 3
    step2_state.point_a.business_type = "SaaS-стартап"
    step2_state.point_a.channels = ["сайт"]
    session.transcript.append({
        "role": "assistant",
        "content": "Сколько у вас сотрудников?",
        "meta": {
            "phase": "point_a",
            "kind": "asker",
            "_state_snapshot": step2_state.model_dump(mode="json"),
        },
    })

    # User answers and state moves forward further
    session.transcript.append({"role": "user", "content": "5 человек"})

    # Step 3: assistant in resources phase — state has more data
    step3_state = WorkflowState()
    step3_state.conversation.phase = Phase.RESOURCES
    step3_state.conversation.language = "ru"
    step3_state.conversation.turn_count = 5
    step3_state.point_a.business_type = "SaaS-стартап"
    step3_state.point_a.channels = ["сайт"]
    step3_state.point_a.team_size = 5
    step3_state.resources.monthly_budget_rub = 30000
    session.transcript.append({
        "role": "assistant",
        "content": "Какой у вас бюджет?",
        "meta": {
            "phase": "resources",
            "kind": "asker",
            "_state_snapshot": step3_state.model_dump(mode="json"),
        },
    })

    # Set current session state to the step3 state (simulating full extraction)
    session.state = step3_state.model_copy(deep=True)
    assert session.state.point_a.business_type == "SaaS-стартап"
    assert session.state.resources.monthly_budget_rub == 30000

    # Undo to step 1 — should restore to step1_state (empty business_type, no budget)
    result = agent.undo(session, target_step=1)

    assert result == "Какой у вас бизнес?"
    assert session.state.point_a.business_type is None
    assert session.state.point_a.channels == []
    assert session.state.resources.monthly_budget_rub is None
    assert session.state.conversation.phase == Phase.POINT_A
    assert session.state.conversation.turn_count == 1


def test_agent_undo_falls_back_without_snapshot(mock_agent_deps):
    """agent.undo() falls back to phase/turn_count reset when no snapshot exists."""
    from app import agent

    # Use _make_session which doesn't include snapshots (old-style sessions)
    session = _make_session()
    session.state.point_a.business_type = "SaaS"

    agent.undo(session, target_step=1)

    # Without snapshot, phase and turn_count are reset but point_a data remains
    assert session.state.conversation.phase == Phase.POINT_A
    assert session.state.conversation.turn_count == 1
    # point_a.business_type is NOT rolled back (legacy behavior)
    assert session.state.point_a.business_type == "SaaS"


def test_session_append_adds_state_snapshot():
    """WorkflowSession.append() automatically adds _state_snapshot for assistant messages."""
    session = WorkflowSession(id="snap-check", workflow_name="v4")
    session.state.conversation.phase = Phase.POINT_A
    session.state.point_a.business_type = "cafe"

    session.append("user", "Hi there")
    session.append("assistant", "Tell me more", meta={"phase": "point_a", "kind": "asker"})

    # User messages should NOT have a snapshot
    assert "_state_snapshot" not in (session.transcript[0].get("meta") or {})

    # Assistant messages should have a snapshot
    assistant_meta = session.transcript[1].get("meta", {})
    assert "_state_snapshot" in assistant_meta
    snapshot = assistant_meta["_state_snapshot"]
    assert snapshot["point_a"]["business_type"] == "cafe"
    assert snapshot["conversation"]["phase"] == "point_a"


def test_session_append_adds_snapshot_even_without_meta():
    """WorkflowSession.append() creates meta dict for snapshot when none provided."""
    session = WorkflowSession(id="snap-check2", workflow_name="v4")
    session.state.point_a.business_type = "shop"

    session.append("assistant", "Hello!")

    # Even without explicit meta, snapshot should be added
    entry = session.transcript[0]
    assert "meta" in entry
    assert "_state_snapshot" in entry["meta"]
    assert entry["meta"]["_state_snapshot"]["point_a"]["business_type"] == "shop"
