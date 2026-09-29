# Adding a business niche to the evaluation set

BizWare's evaluation personas are fictional test participants with an explicit ground-truth ledger. They help check whether a workflow asks useful questions and carries known facts into its final outputs. They are not real customers and are not a substitute for user research.

## Evidence before a persona

For claims about a niche, collect sources before writing the scenario. Prefer primary or authoritative sources. Record one source per material claim with:

- claim and the exact scope it supports;
- source URL, publisher, and date accessed;
- country or market and date range the claim applies to;
- license or terms that govern reuse;
- confidence and any counter-evidence.

Separate sourced observations from hypotheses. Do not copy paywalled text, personal stories, contact details, or proprietary datasets into the repository. Historical source tables without item-level provenance are not runtime guidance and should stay out of the public release until reviewed.

## Create a synthetic hero persona

Use `app/eval/personas.py` as the data model. Each `Persona` needs a fictional name, a short opening message, and a `GroundTruth` ledger covering the business type, channels, tools, pain points, desired outcome, success metric, constraints, budget, LLM access, technical comfort, and maintainer.

Build a plausible composite from the verified claims. Do not model a real named business or reuse a real person's exact story. Make the persona's opening message incomplete enough to exercise discovery; keep additional facts in `GroundTruth` so the judge can check what the workflow recovered. Include uncertainty where evidence does not justify a precise number.

The persona and the judge must remain independent: scenario authors set ground truth before seeing a run's score; reviewers do not rewrite ground truth to make a weak output pass.

## Run the offline comparison

After a maintainer approves the scenario for a local evaluation branch, add it to the `PERSONAS` collection and run the deterministic simulator:

```bash
python -m app.eval.competition --workflows v4 --persona "Persona name"
```

The offline simulator uses `MockLLM`; it checks workflow mechanics and information capture without sending prompts to a provider. Its transcript and heuristic scores are useful signals, not a claim that a real LLM will produce the same conversation. Compare against at least one established persona and inspect the complete transcript and all three final documents where applicable.

For model quality, use the separate live evaluation tools only against a user-owned staging deployment and with the operator's own provider credentials. They can incur charges and write persistent test sessions. Never point them at FiveTOBytes infrastructure or a production database.

## Review and promotion

The independent reviewer reports:

1. which ground-truth facts were recovered, missed, contradicted, or invented;
2. whether the workflow followed its discovery and confirmation checkpoints;
3. whether the outputs are understandable and actionable;
4. known limitations in the source evidence and evaluation method.

A human maintainer checks the sources, license, scenario, transcript, and score before promoting a scenario into committed regression coverage. Keep capability scenarios (new or difficult cases) distinct from established regression cases, and retain a prior passing regression set when a workflow changes.
