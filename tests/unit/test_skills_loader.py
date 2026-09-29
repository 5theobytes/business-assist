"""Tests for app/skills_loader.py — skill file loading."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.skills_loader import SKILL_ORDER, SKILLS_DIR, load_all_skills, load_skill


class TestLoadSkill:
    def test_raises_file_not_found_for_missing_skill(self, tmp_path):
        with patch("app.skills_loader.SKILLS_DIR", tmp_path):
            with pytest.raises(FileNotFoundError, match="Skill not found"):
                load_skill("nonexistent-skill")

    def test_loads_existing_skill(self, tmp_path):
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("# Test Skill\nContent here.", encoding="utf-8")

        with patch("app.skills_loader.SKILLS_DIR", tmp_path):
            result = load_skill("test-skill")

        assert result == "# Test Skill\nContent here."

    def test_reads_utf8_content(self, tmp_path):
        skill_dir = tmp_path / "unicode-skill"
        skill_dir.mkdir()
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text("Привет мир — skill content", encoding="utf-8")

        with patch("app.skills_loader.SKILLS_DIR", tmp_path):
            result = load_skill("unicode-skill")

        assert "Привет мир" in result


class TestLoadAllSkills:
    def test_loads_all_skills_in_order(self, tmp_path):
        for name in SKILL_ORDER:
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(f"content of {name}", encoding="utf-8")

        with patch("app.skills_loader.SKILLS_DIR", tmp_path):
            result = load_all_skills()

        for name in SKILL_ORDER:
            assert f'<skill name="{name}">' in result
            assert f"content of {name}" in result

    def test_raises_if_any_skill_missing(self, tmp_path):
        # Create only the first skill, leave others missing
        d = tmp_path / SKILL_ORDER[0]
        d.mkdir()
        (d / "SKILL.md").write_text("content", encoding="utf-8")

        with patch("app.skills_loader.SKILLS_DIR", tmp_path):
            with pytest.raises(FileNotFoundError):
                load_all_skills()


class TestSkillOrder:
    def test_skill_order_is_nonempty(self):
        assert len(SKILL_ORDER) > 0

    def test_skill_order_has_no_duplicates(self):
        assert len(SKILL_ORDER) == len(set(SKILL_ORDER))
