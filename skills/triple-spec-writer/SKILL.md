---
name: Triple Spec Writer
description: Generates three output documents from a completed business discovery session — a client clarity doc, a developer brief, and a full technical specification with verification checklist.
when_to_use: After all discovery phases (Point A, Point B, Resources) are complete and the owner has confirmed the summary. Final step of the v4 clarity workflow.
version: 1.0.0
language: multilingual
---

# Triple Spec Writer

## Purpose

Generate three distinct output documents from a completed business discovery session.
Each document serves a different audience and purpose, but all are grounded in the same extracted state.

## Output A — Your Clarity Document

A client-facing clarity document written TO the client in second person.

**Style:** Warmest tone, like talking to a friend. No jargon whatsoever.

**Structure:**
1. **Where you are now** — current situation summary (from point_a)
2. **What you want** — desired outcome in the client's own words (from point_b)
3. **Why this matters** — motivation, pain points, what changes in daily life
4. **What you decided** — key decisions made during the session (from decision_log). Each decision as a simple statement: "You chose X because Y"
5. **What's next** — recommended next steps, timeline expectation, what happens after this document

**Rules:**
- Written in second person ("You want...", "You decided...", "Your business...")
- No technical terms: BANNED words — API, webhook, endpoint, SDK, LLM, MVP, backend, middleware, hosting, spec, widget
- Every statement must be traceable to a state field or decision_log entry
- If a section has no data — skip it silently
- Purpose: Client re-reads and confirms "yes, this is exactly what I want"

## Output B — Developer Brief

A non-technical developer brief readable by anyone.

**Style:** Professional but accessible. Plain business language, NO code/tech terms.

**Structure:**
1. **Project Overview** — 1 paragraph summarizing what needs to be built
2. **Features List** — bulleted, plain language, each feature one line
3. **User Stories** — As a [role] I want [action] so that [benefit]
4. **Budget & Timeline constraints** — stated limits from resources
5. **Success Criteria** — measurable outcomes from point_b.success_metric
6. **What's NOT Included** — explicit scope boundaries from point_b.out_of_scope

**Rules:**
- Written FOR a developer but readable by anyone
- No code snippets, no architecture decisions, no technology names
- BANNED: API, webhook, endpoint, SDK, ORM, backend, middleware, cron, schema
- Service names (Telegram, ChatGPT) are fine — they're products, not tech terms
- Each feature/story must map to something in the extracted state
- Purpose: Hand to any developer as a clear brief

## Output C — Technical Specification & Verification Checklist

A full developer-ready specification with numbered requirements.

**Style:** Precise, structured, technical where needed. Technical language is encouraged.

**Structure:**
1. **System Overview** — architecture summary, key components
2. **Functional Requirements** — numbered REQ-001, REQ-002, etc. Each testable.
3. **Non-Functional Requirements** — performance, security, scalability, reliability
4. **Architecture Recommendations** — suggested stack, hosting, patterns
5. **Integrations & External Services** — table of services, purpose, access status
6. **Data Model** — entities, key fields, relationships
7. **API Contracts** — endpoints, webhooks, payload formats (if applicable)
8. **Acceptance Criteria** — per requirement, concrete pass/fail conditions
9. **Verification Checklist** — each REQ mapped to: expected behavior, how to test. Decision log entries MUST appear here as locked decisions.
10. **Open Questions & Assumptions** — anything ambiguous flagged here

**Rules:**
- Technical language is fine and encouraged: API, webhook, endpoint, etc.
- Every REQ must be testable — has a clear pass/fail condition
- Decision log entries become locked constraints in the verification checklist
- Use the algorithm from v2 DEV_SPEC_PROMPT: scan dev_coverage_qa first, then state fields, then sensible defaults with one-line justification
- NO "options" or "variant A/B" — every decision is singular and justified
- Purpose: Developer builds from this; client verifies against checklist

## Language Rules

- Generate in the session's language (ru or en)
- Output A: warmest tone, like talking to a friend
- Output B: professional but accessible
- Output C: precise, structured, technical where needed

## Grounding Rules

- ALL content must be grounded in the extracted state (point_a, point_b, resources, solution, decision_log)
- Never invent features or requirements not discussed
- If something is ambiguous, flag it in "Open Questions" (Output C only)
- Decision log entries MUST appear in:
  - Output A section "What you decided"
  - Output C verification checklist as locked decisions
- dev_coverage_qa entries are architecture decisions — they go into Output C
- solution.shape and solution.shape_description define what we're building
