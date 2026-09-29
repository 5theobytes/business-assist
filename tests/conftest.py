"""Shared test configuration with explicit opt-in for live services."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, MutableMapping

from dotenv import load_dotenv
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_TEST = REPO_ROOT / ".env.test"


def live_test_skip_reason(markers: set[str], env: Mapping[str, str]) -> str | None:
    """Return why a marked live test must be skipped for this environment."""
    if "integration" not in markers:
        return None
    if env.get("BIZWARE_RUN_LIVE_TESTS") != "1":
        return "live tests are disabled; set BIZWARE_RUN_LIVE_TESTS=1 to enable"
    if "requires_anthropic" in markers and not env.get("ANTHROPIC_API_KEY"):
        return "requires ANTHROPIC_API_KEY"
    if "requires_firestore" in markers:
        database = (env.get("FIRESTORE_DATABASE") or "").strip()
        if not database or database.lower() == "(default)":
            return "requires a named non-default FIRESTORE_DATABASE"
        if not (
            env.get("GOOGLE_APPLICATION_CREDENTIALS")
            or env.get("GCP_PROJECT_ID")
        ):
            return "requires GOOGLE_APPLICATION_CREDENTIALS or GCP_PROJECT_ID"
    if "requires_google_speech" in markers and not (
        env.get("GOOGLE_APPLICATION_CREDENTIALS") or env.get("GCP_PROJECT_ID")
    ):
        return "requires Google Cloud credentials for Speech-to-Text"
    return None


def configure_test_environment(env: MutableMapping[str, str]) -> None:
    """Keep default test runs offline, regardless of inherited app settings."""
    if env.get("BIZWARE_RUN_LIVE_TESTS") != "1":
        env["SESSION_STORE"] = "memory"


def pytest_configure(config) -> None:
    if os.environ.get("BIZWARE_RUN_LIVE_TESTS") == "1" and ENV_TEST.exists():
        load_dotenv(ENV_TEST, override=False)
    configure_test_environment(os.environ)
    os.environ.setdefault("FIRESTORE_COLLECTION", "sessions_test")


def pytest_collection_modifyitems(items) -> None:
    for item in items:
        if "tests/integration/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)
        markers = {marker.name for marker in item.iter_markers()}
        reason = live_test_skip_reason(markers, os.environ)
        if reason:
            item.add_marker(pytest.mark.skip(reason=reason))
