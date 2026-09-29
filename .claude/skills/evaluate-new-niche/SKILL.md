---
name: evaluate-new-niche
description: Use when researching a new small-business niche or adding a fictional business-owner scenario to BizWare's offline workflow evaluation. Verify source claims, keep the hero persona synthetic, run the supported evaluation, and require human review before regression promotion.
---

# Evaluate a new business niche

Follow [the repository evaluation guide](../../../docs/evaluation/adding-a-business-niche.md).

1. Identify the niche, market, language, and workflow from the user's request. State any missing scope as an assumption.
2. Delegate source collection to `niche-researcher`. Require source URLs, access dates, geographic scope, reuse terms, confidence, and counter-evidence for material claims. Do not treat missing provenance as a pass.
3. Delegate a fictional scenario draft to `hero-persona-author` using only reviewed research notes. Keep sourced observations, synthetic choices, and uncertainty clearly separated. Do not include real people, companies, customer records, or contact details.
4. Present the proposed persona and its `GroundTruth` ledger for human review before adding it to `app/eval/personas.py`.
5. After approval, run the deterministic offline evaluation against `v4` and at least one established persona. Inspect the full transcript, rubric scores, and generated documents; do not treat the score alone as acceptance.
6. Ask `eval-reviewer` to report evidence against the ground truth without changing the persona, rubric, or expected answers.
7. Summarize source gaps, workflow failures, and unresolved assumptions. A human maintainer decides whether the scenario is a capability case, a regression case, or should be discarded.

Do not call live services unless the user explicitly requests a live evaluation and has configured their own staging endpoint, credentials, and dedicated test database. Never use the private FiveTOBytes endpoint or data.
