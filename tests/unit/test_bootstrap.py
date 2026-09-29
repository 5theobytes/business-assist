"""Tests for app/bootstrap.py — GCP credential materialisation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Wipe every credential-related env var before each test."""
    for k in [
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCP_PROJECT_ID",
        "GCP_CLIENT_EMAIL",
        "GCP_PRIVATE_KEY",
        "GCP_PRIVATE_KEY_ID",
    ]:
        monkeypatch.delenv(k, raising=False)
    # also reset module-level cache
    import app.bootstrap as b
    b._MATERIALISED = None


def test_returns_none_when_env_empty():
    from app.bootstrap import materialise_gcp_credentials

    result = materialise_gcp_credentials()

    assert result is None
    assert os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") is None


def test_noop_when_credentials_path_already_set(monkeypatch, tmp_path):
    fake_sa = tmp_path / "fake-sa.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake_sa))
    monkeypatch.setenv("GCP_PROJECT_ID", "x")
    monkeypatch.setenv("GCP_CLIENT_EMAIL", "x@x.iam.gserviceaccount.com")
    monkeypatch.setenv("GCP_PRIVATE_KEY", "fake-test-key\\n")

    from app.bootstrap import materialise_gcp_credentials

    result = materialise_gcp_credentials()

    assert result is None
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(fake_sa)


def test_writes_temp_file_when_individual_fields_set(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "test-proj")
    monkeypatch.setenv("GCP_CLIENT_EMAIL", "runner@test-proj.iam.gserviceaccount.com")
    # \\n in the literal — must be unescaped to real newlines
    monkeypatch.setenv(
        "GCP_PRIVATE_KEY",
        "test-key-part-one\\ntest-key-part-two\\n",
    )

    from app.bootstrap import materialise_gcp_credentials
    import json

    result = materialise_gcp_credentials()

    assert result is not None
    assert result.exists()
    payload = json.loads(result.read_text(encoding="utf-8"))
    assert payload["type"] == "service_account"
    assert payload["project_id"] == "test-proj"
    assert payload["client_email"] == "runner@test-proj.iam.gserviceaccount.com"
    assert payload["private_key"] == "test-key-part-one\ntest-key-part-two\n"
    assert "\\n" not in payload["private_key"]  # real newlines, not escapes
    assert payload["token_uri"] == "https://oauth2.googleapis.com/token"
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(result)


def test_idempotent_does_not_create_second_file(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    monkeypatch.setenv("GCP_CLIENT_EMAIL", "e@p.iam.gserviceaccount.com")
    monkeypatch.setenv("GCP_PRIVATE_KEY", "fake-test-key\\n")

    from app.bootstrap import materialise_gcp_credentials

    first = materialise_gcp_credentials()
    second = materialise_gcp_credentials()

    assert first == second
    assert first is not None and first.exists()
