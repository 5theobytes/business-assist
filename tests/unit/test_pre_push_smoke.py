"""Smoke test: pre_push script imports and parses args."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_pre_push_help_exits_zero():
    repo = Path(__file__).resolve().parents[2]
    r = subprocess.run(
        [sys.executable, str(repo / "scripts" / "pre_push.py"), "--help"],
        capture_output=True, text=True, timeout=15,
    )
    assert r.returncode == 0
    assert "pre_push" in r.stdout.lower() or "usage" in r.stdout.lower()
