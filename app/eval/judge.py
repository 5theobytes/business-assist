"""Scoring and judging.

Two layers:

1. **Heuristic judge** (`score_simulation`) — deterministic, no LLM.
   Compares the workflow's final state and produced specs against the persona's
   ground truth. Reports:
     - coverage_a / coverage_b / coverage_resources (fraction of GT facts captured)
     - dev_spec_quality (presence of expected sections + GT facts mentioned)
     - biz_spec_friendliness (jargon penalty + length sanity)
     - efficiency (turns to completion, normalized)
     - one-question-at-a-time discipline (% of bot messages with one '?')

2. **LLM-judge interface** (`build_judge_prompt`) — produces a self-contained
   prompt that an external Claude/agent can use for a side-by-side comparison.
   We don't call the LLM here — the orchestration script does (using the Agent
   tool).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .personas import GroundTruth
from .simulator import SimulationResult


# ---------- heuristic scoring ----------


JARGON_TERMS = [
    "API", "REST", "GraphQL", "OAuth", "webhook", "endpoint", "SDK", "ORM", "TBD",
    "JWT", "DTO", "lambda", "regex", "deploy", "CI/CD", "MVC",
]


@dataclass
class HeuristicScores:
    coverage_a: float = 0.0
    coverage_b: float = 0.0
    coverage_resources: float = 0.0
    dev_spec_quality: float = 0.0
    biz_spec_friendliness: float = 0.0
    efficiency: float = 0.0
    one_q_discipline: float = 0.0
    total: float = 0.0
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "coverage_a": round(self.coverage_a, 3),
            "coverage_b": round(self.coverage_b, 3),
            "coverage_resources": round(self.coverage_resources, 3),
            "dev_spec_quality": round(self.dev_spec_quality, 3),
            "biz_spec_friendliness": round(self.biz_spec_friendliness, 3),
            "efficiency": round(self.efficiency, 3),
            "one_q_discipline": round(self.one_q_discipline, 3),
            "total": round(self.total, 3),
            "details": self.details,
        }


def _coverage(state_section: dict, gt_dict: dict, fields: list[str]) -> float:
    if not fields:
        return 1.0
    hits = 0
    for f in fields:
        gt_v = gt_dict.get(f)
        st_v = state_section.get(f)
        if gt_v in (None, "", []):
            hits += 1  # nothing to capture
            continue
        if isinstance(gt_v, list):
            # at least half of GT items present (case-insensitive substring)
            sv = [s.lower() for s in (st_v or [])]
            matched = sum(1 for g in gt_v if any(g.lower() in s or s in g.lower() for s in sv))
            if matched >= max(1, len(gt_v) // 2):
                hits += 1
        else:
            if st_v in (None, ""):
                continue
            if isinstance(st_v, str) and (str(gt_v).lower() in st_v.lower() or st_v.lower() in str(gt_v).lower()):
                hits += 1
            elif st_v == gt_v:
                hits += 1
            elif isinstance(gt_v, int) and isinstance(st_v, int) and abs(gt_v - st_v) / max(1, gt_v) < 0.3:
                hits += 1
    return hits / len(fields)


def _dev_spec_quality(dev_spec: str | None, gt: GroundTruth) -> tuple[float, dict]:
    if not dev_spec:
        return 0.0, {"reason": "no dev spec produced"}
    expected_sections = [
        "## Goal", "## Functional requirements", "## Architecture sketch",
        "## Integrations", "## Acceptance criteria", "## Out of scope",
        "## Resources", "## Open questions",
    ]
    section_score = sum(1 for s in expected_sections if s in dev_spec) / len(expected_sections)

    # GT facts mentioned
    facts = []
    for ch in gt.channels:
        facts.append(ch.lower() in dev_spec.lower())
    for tool in gt.tools_in_use:
        facts.append(tool.lower() in dev_spec.lower())
    if gt.success_metric:
        # check at least 2 keywords match
        kws = [k for k in re.split(r"\W+", gt.success_metric.lower()) if len(k) > 3]
        facts.append(sum(1 for k in kws if k in dev_spec.lower()) >= 2)
    if gt.out_of_scope:
        facts.append(any(x.lower() in dev_spec.lower() for x in gt.out_of_scope))
    fact_score = sum(facts) / max(1, len(facts))

    score = 0.5 * section_score + 0.5 * fact_score
    return score, {"sections_score": round(section_score, 3), "facts_score": round(fact_score, 3)}


def _biz_spec_friendliness(biz_spec: str | None) -> tuple[float, dict]:
    if not biz_spec:
        return 0.0, {"reason": "no business spec produced"}
    expected = ["## Зачем", "## Главная цель", "## Как сейчас", "## Как будет",
                 "## Что мы НЕ делаем"]
    sections = sum(1 for s in expected if s in biz_spec) / len(expected)
    jargon_hits = sum(1 for term in JARGON_TERMS if re.search(rf"\b{re.escape(term)}\b", biz_spec))
    jargon_penalty = max(0.0, 1.0 - jargon_hits * 0.1)
    return 0.6 * sections + 0.4 * jargon_penalty, {
        "sections_score": round(sections, 3),
        "jargon_hits": jargon_hits,
    }


def _efficiency(result: SimulationResult) -> tuple[float, dict]:
    """Bell-shaped: rewards 8–14 turns, penalizes too short or too long."""
    n = result.turn_count
    if n == 0:
        return 0.0, {"turn_count": 0}
    if 8 <= n <= 14:
        score = 1.0
    elif n < 8:
        score = max(0.0, n / 8)
    else:
        score = max(0.0, 1.0 - (n - 14) / 12)
    return score, {"turn_count": n}


def _one_q_discipline(result: SimulationResult) -> tuple[float, dict]:
    bot_msgs = [m["content"] for m in result.transcript if m["role"] == "assistant"]
    # Exclude opening greeting and final spec dump (long composite outputs).
    middle = bot_msgs[1:-1] if len(bot_msgs) > 2 else bot_msgs
    if not middle:
        return 0.0, {"middle_msgs": 0}
    one_q = sum(1 for m in middle if m.count("?") == 1)
    return one_q / len(middle), {"middle_msgs": len(middle), "one_q": one_q}


def score_simulation(result: SimulationResult, gt: GroundTruth) -> HeuristicScores:
    """Score a simulation result against the persona's ground truth."""
    state = result.final_state or {}
    pa, pb, pr = state.get("point_a", {}), state.get("point_b", {}), state.get("resources", {})
    gt_a = {"business_type": gt.business_type, "channels": gt.channels,
            "tools_in_use": gt.tools_in_use, "pain_points": gt.pain_points}
    gt_b = {"primary_value": gt.primary_value, "desired_outcome": gt.desired_outcome,
            "success_metric": gt.success_metric, "out_of_scope": gt.out_of_scope}
    gt_r = {"monthly_budget_rub": gt.monthly_budget_rub, "has_llm_key": gt.has_llm_key,
            "tech_savviness": gt.tech_savviness, "maintainer": gt.maintainer}

    cov_a = _coverage(pa, gt_a, list(gt_a))
    cov_b = _coverage(pb, gt_b, list(gt_b))
    cov_r = _coverage(pr, gt_r, list(gt_r))
    dev_q, dev_d = _dev_spec_quality(result.dev_spec, gt)
    biz_q, biz_d = _biz_spec_friendliness(result.business_spec)
    eff, eff_d = _efficiency(result)
    oqd, oqd_d = _one_q_discipline(result)

    weights = {
        "coverage_a": 0.18,
        "coverage_b": 0.18,
        "coverage_resources": 0.12,
        "dev_spec_quality": 0.22,
        "biz_spec_friendliness": 0.10,
        "efficiency": 0.10,
        "one_q_discipline": 0.10,
    }
    total = (
        cov_a * weights["coverage_a"]
        + cov_b * weights["coverage_b"]
        + cov_r * weights["coverage_resources"]
        + dev_q * weights["dev_spec_quality"]
        + biz_q * weights["biz_spec_friendliness"]
        + eff * weights["efficiency"]
        + oqd * weights["one_q_discipline"]
    )
    return HeuristicScores(
        coverage_a=cov_a,
        coverage_b=cov_b,
        coverage_resources=cov_r,
        dev_spec_quality=dev_q,
        biz_spec_friendliness=biz_q,
        efficiency=eff,
        one_q_discipline=oqd,
        total=total,
        details={"dev": dev_d, "biz": biz_d, "eff": eff_d, "oq": oqd_d, "weights": weights},
    )


# ---------- LLM judge prompt builder ----------


JUDGE_SYSTEM = """Ты — независимый эксперт по продуктовому discovery и техническим спекам. \
Твоя задача — оценить, какой из трёх воркфлоу провёл лучшее интервью и подготовил лучшие спеки. \
Будь строг и конкретен. Используй рубрику. Возвращай JSON.

Рубрика (10-балльная по каждому пункту):
1. user_friendliness — насколько комфортно и ясно было предпринимателю-нетехнарю
2. discovery_depth — насколько глубоко выяснены боли (Mom-Test: прошлое поведение, не гипотезы)
3. coverage — насколько полно покрыта Точка А, Точка Б и ресурсы
4. dev_spec_quality — спека для разработчика: конкретика, AC, integrations, out of scope
5. biz_spec_clarity — бизнес-версия: ясность, отсутствие жаргона, наблюдаемые "было → стало"
6. efficiency — без "воды", без лишних вопросов, разумное число ходов
"""


def build_judge_prompt(results: list[tuple[str, SimulationResult]]) -> str:
    """Build a single prompt for an LLM judge to compare all workflows side-by-side."""
    sections = []
    for label, r in results:
        transcript_md = "\n".join(
            f"**{m['role'].upper()}:** {m['content']}" for m in r.transcript
        )
        sections.append(
            f"## Версия {label} ({r.workflow_name}) — {r.turn_count} ходов\n\n"
            f"### Transcript\n\n{transcript_md}\n\n"
            f"### Business spec\n\n{r.business_spec or '_(не сгенерирована)_'}\n\n"
            f"### Dev spec\n\n{r.dev_spec or '_(не сгенерирована)_'}\n"
        )
    body = "\n\n---\n\n".join(sections)
    schema = """{
  "scores": {
    "v1": {"user_friendliness": int, "discovery_depth": int, "coverage": int,
            "dev_spec_quality": int, "biz_spec_clarity": int, "efficiency": int, "total": int},
    "v2": {...same fields...},
    "v3": {...same fields...}
  },
  "ranking": ["best", "middle", "worst"],
  "reasoning": "коротко: почему такое ранжирование, что v1/v2/v3 сделали по-разному",
  "actionable_improvements": ["конкретные правки для победителя"]
}"""
    return (
        body
        + "\n\n---\n\n## Задача судьи\n"
        + "Оцени каждую версию по рубрике. Сравни друг с другом. Верни ТОЛЬКО JSON по схеме:\n\n"
        + schema
    )
