"""Loads SKILL.md files from the skills/ directory and assembles them into a system prompt."""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"

SKILL_ORDER = [
    "conversational-discovery",
    "business-discovery",
    "business-goals",
    "resource-discovery",
    "dual-spec-writer",
    "triple-spec-writer",
]


def load_skill(name: str) -> str:
    skill_path = SKILLS_DIR / name / "SKILL.md"
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_path}")
    return skill_path.read_text(encoding="utf-8")


def load_all_skills() -> str:
    blocks = []
    for name in SKILL_ORDER:
        blocks.append(f"<skill name=\"{name}\">\n{load_skill(name)}\n</skill>")
    return "\n\n".join(blocks)
