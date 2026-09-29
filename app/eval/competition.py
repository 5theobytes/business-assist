"""Run the competition: every workflow vs every persona, score, write a report.

Usage:
    python -m app.eval.competition                  # mock LLM, all personas
    python -m app.eval.competition --persona Анна
    python -m app.eval.competition --workflows v2 v3
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .judge import HeuristicScores, build_judge_prompt, score_simulation
from .personas import PERSONAS, Persona
from .simulator import SimulationResult, simulate

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = REPO_ROOT / "eval_runs"


def run_one(workflow_name: str, persona: Persona, max_turns: int = 25) -> SimulationResult:
    return simulate(workflow_name, persona, max_turns=max_turns)


def run_competition(
    workflows: list[str], personas: list[Persona], max_turns: int = 25
) -> dict:
    results: dict[str, dict[str, SimulationResult]] = {}
    scores: dict[str, dict[str, HeuristicScores]] = {}
    for wf in workflows:
        results[wf] = {}
        scores[wf] = {}
        for p in personas:
            r = run_one(wf, p, max_turns=max_turns)
            results[wf][p.name] = r
            scores[wf][p.name] = score_simulation(r, p.ground_truth)
    return {"results": results, "scores": scores}


def write_report(competition: dict, out_path: Path) -> None:
    results = competition["results"]
    scores = competition["scores"]
    workflows = list(results.keys())
    persona_names = sorted({p for wf in results for p in results[wf]})

    lines: list[str] = []
    lines.append("# Workflow Competition — heuristic scores\n")
    lines.append(
        "Each workflow was simulated against scripted personas using a deterministic "
        "MockLLM (no real Claude calls). Scores come from the heuristic judge that "
        "compares the workflow's final state and produced specs against the persona's "
        "ground truth.\n"
    )

    # Aggregate totals per workflow
    lines.append("## Aggregate totals\n")
    lines.append("| Workflow | Avg total | " + " | ".join(persona_names) + " |")
    lines.append("| --- | --- | " + " | ".join(["---"] * len(persona_names)) + " |")
    for wf in workflows:
        per = [scores[wf][p].total for p in persona_names]
        avg = sum(per) / len(per)
        lines.append(
            f"| **{wf}** | **{avg:.3f}** | "
            + " | ".join(f"{x:.3f}" for x in per)
            + " |"
        )
    lines.append("")

    # Per-persona breakdown
    for p in persona_names:
        lines.append(f"## Persona: {p}\n")
        lines.append("| Metric | " + " | ".join(workflows) + " |")
        lines.append("| --- | " + " | ".join(["---"] * len(workflows)) + " |")
        sample_metrics = [
            "coverage_a", "coverage_b", "coverage_resources",
            "dev_spec_quality", "biz_spec_friendliness",
            "efficiency", "one_q_discipline", "total",
        ]
        for metric in sample_metrics:
            row = [f"**{metric}**"]
            best = max(getattr(scores[wf][p], metric) for wf in workflows)
            for wf in workflows:
                v = getattr(scores[wf][p], metric)
                marker = " 🏆" if v == best and v > 0 else ""
                row.append(f"{v:.3f}{marker}")
            lines.append("| " + " | ".join(row) + " |")
        lines.append("")
        # also turn count
        for wf in workflows:
            lines.append(
                f"- {wf}: {results[wf][p].turn_count} turns, completed={results[wf][p].completed}"
            )
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflows", nargs="*", default=["v1", "v2", "v3"])
    ap.add_argument("--persona", default=None, help="filter to one persona by name substring")
    ap.add_argument("--max-turns", type=int, default=25)
    ap.add_argument("--out", default=str(RUNS_DIR / "report.md"))
    args = ap.parse_args()

    chosen = PERSONAS
    if args.persona:
        chosen = [p for p in PERSONAS if args.persona.lower() in p.name.lower()]
        if not chosen:
            raise SystemExit(f"no persona matching '{args.persona}'")

    competition = run_competition(args.workflows, chosen, max_turns=args.max_turns)

    RUNS_DIR.mkdir(exist_ok=True)
    out = Path(args.out)
    write_report(competition, out)

    # Also dump raw transcripts as JSON for offline inspection.
    raw = {
        "workflows": args.workflows,
        "personas": [p.name for p in chosen],
        "results": {
            wf: {
                p.name: {
                    "transcript": competition["results"][wf][p.name].transcript,
                    "final_state": competition["results"][wf][p.name].final_state,
                    "business_spec": competition["results"][wf][p.name].business_spec,
                    "dev_spec": competition["results"][wf][p.name].dev_spec,
                    "turn_count": competition["results"][wf][p.name].turn_count,
                    "completed": competition["results"][wf][p.name].completed,
                    "scores": competition["scores"][wf][p.name].as_dict(),
                }
                for p in chosen
            }
            for wf in args.workflows
        },
    }
    (RUNS_DIR / "raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} and {RUNS_DIR / 'raw.json'}")


if __name__ == "__main__":
    main()
