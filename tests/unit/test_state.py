"""Tests for the canonical state model: coverage, gap detection, phase routing."""
from app.state import Phase, PointA, PointB, Resources, WorkflowState


def test_empty_state_has_zero_coverage():
    s = WorkflowState()
    assert s.coverage_a() == 0.0
    assert s.coverage_b() == 0.0
    assert s.coverage_resources() == 0.0


def test_partial_point_a_coverage():
    s = WorkflowState(point_a=PointA(business_type="магазин", channels=["telegram"]))
    # 2 of 4 required fields filled (business_type, channels)
    assert 0.49 <= s.coverage_a() <= 0.51


def test_full_point_a_coverage():
    s = WorkflowState(point_a=PointA(
        business_type="магазин",
        channels=["telegram"],
        tools_in_use=["excel"],
        pain_points=["теряю заявки"],
    ))
    assert s.coverage_a() == 1.0


def test_open_gaps_for_point_a():
    s = WorkflowState(point_a=PointA(business_type="магазин"))
    gaps = s.open_gaps(Phase.POINT_A)
    assert "business_type" not in gaps
    assert "channels" in gaps
    assert "tools_in_use" in gaps
    assert "pain_points" in gaps


def test_phase_routing_advances_when_complete():
    s = WorkflowState(
        point_a=PointA(
            business_type="магазин",
            channels=["telegram"],
            tools_in_use=["excel"],
            pain_points=["теряю"],
        ),
    )
    s.conversation.phase = Phase.POINT_A
    assert s.next_phase() == Phase.POINT_B


def test_phase_routing_stays_when_incomplete():
    s = WorkflowState(point_a=PointA(business_type="магазин"))
    s.conversation.phase = Phase.POINT_A
    assert s.next_phase() == Phase.POINT_A


def test_greeting_always_advances_to_point_a():
    s = WorkflowState()
    s.conversation.phase = Phase.GREETING
    assert s.next_phase() == Phase.POINT_A


def test_done_only_after_summary_confirmed():
    s = WorkflowState()
    s.conversation.phase = Phase.SPEC_REVIEW
    assert s.next_phase() == Phase.SPEC_REVIEW
    s.conversation.confirmed_summary = True
    assert s.next_phase() == Phase.DONE


def test_conversation_timestamps_default_none():
    s = WorkflowState()
    assert s.conversation.started_at is None
    assert s.conversation.ended_at is None


def test_conversation_timestamps_roundtrip_through_dump():
    from datetime import datetime, timezone
    started = datetime(2026, 5, 7, 14, 0, tzinfo=timezone.utc)
    ended = datetime(2026, 5, 7, 14, 23, tzinfo=timezone.utc)
    s = WorkflowState()
    s.conversation.started_at = started
    s.conversation.ended_at = ended
    payload = s.model_dump(mode="json")
    s2 = WorkflowState.model_validate(payload)
    assert s2.conversation.started_at == started
    assert s2.conversation.ended_at == ended


def test_new_owner_fields_default_empty():
    s = WorkflowState()
    assert s.point_b.user_flows == []
    assert s.point_b.functional_requirements_owner == []
    assert s.resources.deployment_constraints == []
    assert s.resources.preferred_channel is None


def test_new_owner_fields_roundtrip_through_dump():
    s = WorkflowState(
        point_b=PointB(
            user_flows=["клиент пишет — бот отвечает"],
            functional_requirements_owner=["должен сам отвечать ночью"],
        ),
        resources=Resources(
            deployment_constraints=["сайт уже на Tilda"],
            preferred_channel="telegram",
        ),
    )
    payload = s.model_dump(mode="json")
    s2 = WorkflowState.model_validate(payload)
    assert s2.point_b.user_flows == ["клиент пишет — бот отвечает"]
    assert s2.point_b.functional_requirements_owner == ["должен сам отвечать ночью"]
    assert s2.resources.deployment_constraints == ["сайт уже на Tilda"]
    assert s2.resources.preferred_channel == "telegram"
