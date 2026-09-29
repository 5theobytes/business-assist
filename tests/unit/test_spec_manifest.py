"""Tests for app/spec_manifest — the OWNER/DEV/MIXED classification and helpers."""
from __future__ import annotations

from app.spec_manifest import (
    MANIFEST,
    collect_pending_decisions,
    fields_for_phase,
    is_phase_complete,
    next_owner_field,
    owner_gaps,
)
from app.state import Phase, PointA, PointB, Resources, WorkflowState


def test_every_field_has_classification():
    for f in MANIFEST:
        assert f.classification in ("OWNER", "DEV", "MIXED"), f.name


def test_owner_and_mixed_fields_have_owner_question():
    for f in MANIFEST:
        if f.classification in ("OWNER", "MIXED"):
            assert f.owner_question, f"{f.name} missing owner_question"
            assert f.phase is not None, f"{f.name} missing phase"
            assert f.state_path is not None, f"{f.name} missing state_path"


def test_dev_fields_have_decision_topic_and_no_owner_side():
    for f in MANIFEST:
        if f.classification == "DEV":
            assert f.decision_topic, f"{f.name} missing decision_topic"
            assert f.owner_question is None, f"{f.name} should not have owner_question"
            assert f.state_path is None, f"{f.name} should not have state_path"


def test_field_names_are_unique():
    names = [f.name for f in MANIFEST]
    assert len(names) == len(set(names))


def test_fields_for_phase_returns_sorted():
    fields = fields_for_phase(Phase.POINT_A)
    priorities = [f.priority for f in fields]
    assert priorities == sorted(priorities)
    # Все возвращённые — OWNER или MIXED, не DEV
    for f in fields:
        assert f.classification in ("OWNER", "MIXED")


def test_owner_gaps_excludes_filled_fields():
    state = WorkflowState(
        point_a=PointA(business_type="магазин", channels=["telegram"]),
    )
    gaps = owner_gaps(state, Phase.POINT_A)
    gap_names = {f.name for f in gaps}
    assert "business_type" not in gap_names
    assert "channels" not in gap_names
    assert "tools_in_use" in gap_names
    assert "pain_points" in gap_names


def test_next_owner_field_returns_highest_priority_gap():
    state = WorkflowState()
    nxt = next_owner_field(state, Phase.POINT_A)
    assert nxt is not None
    assert nxt.name == "business_type"  # priority 1


def test_next_owner_field_skips_filled_top_priority():
    state = WorkflowState(point_a=PointA(business_type="магазин"))
    nxt = next_owner_field(state, Phase.POINT_A)
    assert nxt is not None
    assert nxt.name == "stage"  # business_type filled → next is priority 2


def test_next_owner_field_returns_none_when_phase_done():
    state = WorkflowState(
        point_a=PointA(
            business_type="магазин",
            stage="работает",
            team_size=2,
            channels=["telegram"],
            tools_in_use=["excel"],
            pain_points=["теряю заявки"],
            daily_volume="5",
        ),
    )
    assert next_owner_field(state, Phase.POINT_A) is None


def test_is_phase_complete_strict_on_owner_fields():
    s = WorkflowState(point_a=PointA(business_type="магазин"))
    assert not is_phase_complete(s, Phase.POINT_A)

    s2 = WorkflowState(
        point_a=PointA(
            business_type="магазин",
            stage="работает",
            team_size=2,
            channels=["telegram"],
            tools_in_use=["excel"],
            pain_points=["теряю заявки"],
            daily_volume="5",
        ),
    )
    assert is_phase_complete(s2, Phase.POINT_A)


def test_collect_pending_decisions_returns_empty_list():
    """No unresolved architectural decisions remain after coverage review.

    The function remains available to preserve its existing caller contract.
    """
    state = WorkflowState()
    assert collect_pending_decisions(state) == []


def test_collect_pending_decisions_returns_empty_for_filled_state():
    state = WorkflowState(
        point_b=PointB(user_flows=["клиент пишет — бот отвечает"]),
    )
    assert collect_pending_decisions(state) == []


def test_resources_phase_fields_match_manifest():
    fields = fields_for_phase(Phase.RESOURCES)
    field_names = {f.name for f in fields}
    # хотя бы базовые ресурсные поля
    assert "monthly_budget_rub" in field_names
    assert "tech_savviness" in field_names
    assert "maintainer" in field_names
    # новые поля из расширения state
    assert "deployment_constraints" in field_names
    assert "preferred_channel" in field_names


def test_point_b_phase_fields_include_new_owner_questions():
    fields = fields_for_phase(Phase.POINT_B)
    field_names = {f.name for f in fields}
    # старые поля
    assert "primary_value" in field_names
    assert "out_of_scope" in field_names
    # новые
    assert "user_flows" in field_names
    assert "functional_requirements_owner" in field_names
