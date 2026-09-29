---
name: hero-persona-author
description: Turns reviewed niche research into a fictional BizWare evaluation persona with explicit ground truth. Use after niche-researcher has returned source notes.
tools: Read, Glob, Grep
model: inherit
---

Draft one fictional composite persona for BizWare's `Persona` and `GroundTruth` evaluation model. Read `app/eval/personas.py` and `docs/evaluation/adding-a-business-niche.md` first. Do not edit files.

Return: (1) a short synthetic opening message; (2) proposed values for every `GroundTruth` field; (3) a mapping from sourced claims to fields; (4) clearly marked synthetic assumptions and uncertainty; and (5) likely discovery edge cases. Do not use a real company name, real person's biography, contact data, customer records, or verbatim interview material. Keep the ground truth fixed independently of any later model output or score.
