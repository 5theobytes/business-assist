"""Heuristic judge sanity tests."""
from app.eval.judge import build_judge_prompt, score_simulation
from app.eval.personas import PERSONAS
from app.eval.simulator import SimulationResult


def _anna():
    return next(p for p in PERSONAS if "Анна" in p.name)


def test_empty_simulation_scores_zero():
    p = _anna()
    r = SimulationResult(workflow_name="x", persona_name=p.name, transcript=[],
                          final_state={}, business_spec=None, dev_spec=None,
                          completed=False, turn_count=0)
    s = score_simulation(r, p.ground_truth)
    assert s.total < 0.1


def test_full_match_scores_high():
    p = _anna()
    gt = p.ground_truth
    state = {
        "point_a": {
            "business_type": gt.business_type, "channels": gt.channels,
            "tools_in_use": gt.tools_in_use, "pain_points": gt.pain_points,
        },
        "point_b": {
            "primary_value": gt.primary_value, "desired_outcome": gt.desired_outcome,
            "success_metric": gt.success_metric, "out_of_scope": gt.out_of_scope,
        },
        "resources": {
            "monthly_budget_rub": gt.monthly_budget_rub, "has_llm_key": gt.has_llm_key,
            "tech_savviness": gt.tech_savviness, "maintainer": gt.maintainer,
        },
    }
    biz = "## Зачем\nx\n## Главная цель\nx\n## Как сейчас\nx\n## Как будет\nx\n## Что мы НЕ делаем\nx"
    dev = (
        "## Goal\nx\n## Functional requirements\nx\n## Architecture sketch\nx\n"
        "## Integrations\nx\n## Acceptance criteria\nx\n## Out of scope\nx\n"
        "## Resources\nx\n## Open questions\nx\n"
        f"channels: {' '.join(gt.channels)}; tools: {' '.join(gt.tools_in_use)};"
        f" {gt.success_metric}; {' '.join(gt.out_of_scope)}"
    )
    transcript = [{"role": "assistant", "content": "Q1?"}, {"role": "user", "content": "A1"}] * 12
    r = SimulationResult(
        workflow_name="x", persona_name=p.name, transcript=transcript,
        final_state=state, business_spec=biz, dev_spec=dev,
        completed=True, turn_count=12,
    )
    s = score_simulation(r, p.ground_truth)
    assert s.coverage_a >= 0.75
    assert s.coverage_b >= 0.75
    assert s.coverage_resources >= 0.75
    assert s.total > 0.7


def test_jargon_reduces_biz_friendliness():
    p = _anna()
    gt = p.ground_truth
    biz_clean = "## Зачем\nудобно\n## Главная цель\nx\n## Как сейчас\nx\n## Как будет\nx\n## Что мы НЕ делаем\nx"
    biz_jargon = biz_clean + "\nИспользуем REST API через webhook и OAuth с JWT."
    r1 = SimulationResult(workflow_name="x", persona_name=p.name, transcript=[],
                           final_state={}, business_spec=biz_clean, dev_spec=None,
                           completed=False, turn_count=10)
    r2 = SimulationResult(workflow_name="x", persona_name=p.name, transcript=[],
                           final_state={}, business_spec=biz_jargon, dev_spec=None,
                           completed=False, turn_count=10)
    s1 = score_simulation(r1, gt)
    s2 = score_simulation(r2, gt)
    assert s1.biz_spec_friendliness > s2.biz_spec_friendliness


def test_efficiency_bell_curve():
    p = _anna()
    gt = p.ground_truth
    short = SimulationResult(workflow_name="x", persona_name=p.name, transcript=[],
                              final_state={}, business_spec=None, dev_spec=None,
                              completed=False, turn_count=2)
    sweet = SimulationResult(workflow_name="x", persona_name=p.name, transcript=[],
                              final_state={}, business_spec=None, dev_spec=None,
                              completed=True, turn_count=10)
    long = SimulationResult(workflow_name="x", persona_name=p.name, transcript=[],
                              final_state={}, business_spec=None, dev_spec=None,
                              completed=False, turn_count=25)
    assert score_simulation(sweet, gt).efficiency > score_simulation(short, gt).efficiency
    assert score_simulation(sweet, gt).efficiency > score_simulation(long, gt).efficiency


def test_judge_prompt_has_all_workflows():
    p = _anna()
    rs = []
    for wf in ("v1", "v2", "v3"):
        rs.append((wf, SimulationResult(
            workflow_name=wf, persona_name=p.name,
            transcript=[{"role": "assistant", "content": "hi"}, {"role": "user", "content": "hello"}],
            final_state={}, business_spec="biz", dev_spec="dev",
            completed=True, turn_count=10,
        )))
    prompt = build_judge_prompt(rs)
    assert "v1" in prompt and "v2" in prompt and "v3" in prompt
    assert "JSON" in prompt
