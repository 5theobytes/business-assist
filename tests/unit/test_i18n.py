"""Bilingual (i18n) integration tests.

Six tests covering:
1. Language persists across session load (HTTP round-trip)
2. Bot replies in EN when session lang is EN (system prompt check)
3. Banlist enforced in EN output
4. Postprocess does NOT ru-sanitize EN output
5. Accept-Language header detection
6. Summary template renders EN with USD
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "supersecret")
    monkeypatch.setenv("ADMIN_TOKEN", "admintoken")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TEST:TOKEN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    from app.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Test 1: language persists across session load
# ---------------------------------------------------------------------------

def test_language_persists_across_session_load(client):
    """POST /api/session with Accept-Language: en-US sets state.language='en';
    GET /api/session/{sid} returns language='en'."""
    # Create session with English language header
    r = client.post(
        "/api/session",
        json={"language": "en"},
        headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["language"] == "en"
    sid = data["session_id"]
    assert sid != "unconfigured"

    # Load the session and verify language echoed back
    r2 = client.get(f"/api/session/{sid}")
    assert r2.status_code == 200
    assert r2.json()["language"] == "en"


# ---------------------------------------------------------------------------
# Test 2: bot replies in EN when session lang is EN
# ---------------------------------------------------------------------------

def test_bot_replies_in_en_when_state_lang_en(monkeypatch):
    """WorkflowSession with language='en'. Mock LLM captures system prompt.
    Assert that the system prompt fed to the LLM contains a substring of
    CONVERSATIONAL_STYLE['en'] (e.g. 'You are Business Assist')."""
    from app.llm import MockLLM
    from app.workflows import build
    from app.workflows.common_prompts import CONVERSATIONAL_STYLE

    captured_systems: list[str] = []

    def recording_responder(system: str, messages: list, tools=None) -> str:
        captured_systems.append(system or "")
        # Return a tool_use structure if tools are requested (extractor call)
        # otherwise return plain text (asker call)
        if tools:
            return ""  # MockLLM will parse this as no tool call
        return "Tell me more about your business."

    llm = MockLLM(responder=recording_responder)
    wf = build("v2", llm=llm)
    session = wf.new_session()
    # Set language to EN before start
    session.state.conversation.language = "en"

    wf.start(session)
    # respond() triggers the asker LLM call with the system prompt
    wf.respond(session, "I run an online store selling handmade crafts")

    # At least one captured system prompt should contain the EN style marker
    en_style_marker = "You are Business Assist"
    assert any(en_style_marker in s for s in captured_systems), (
        f"Expected EN style marker '{en_style_marker}' in system prompts. "
        f"Got prompts starting with: {[s[:80] for s in captured_systems]}"
    )


# ---------------------------------------------------------------------------
# Test 3: banlist enforced in EN output
# ---------------------------------------------------------------------------

def test_banlist_enforced_in_en_output():
    """check() with lang='en' flags 'API' and 'webhook' as banned_term violations."""
    from app.workflows.postprocess import check

    text = "we will connect via the API webhook endpoint"
    violations = check(text, lang="en")
    banned_matches = {v.match.lower() for v in violations if v.rule == "banned_term"}

    assert "api" in banned_matches, f"Expected 'api' banned. Got: {banned_matches}"
    assert "webhook" in banned_matches, f"Expected 'webhook' banned. Got: {banned_matches}"


# ---------------------------------------------------------------------------
# Test 4: postprocess does NOT ru-sanitize EN output
# ---------------------------------------------------------------------------

def test_postprocess_does_not_ru_sanitize_en_output():
    """sanitize() with lang='en' replaces EN-specific jargon (widget→website chat,
    hosting→server for the bot) but does NOT touch English words like 'developer'."""
    from app.workflows.postprocess import sanitize

    text = "the developer will set up the widget for hosting"
    out = sanitize(text, lang="en")

    # 'developer' is explicitly NOT replaced in EN (reads neutral)
    assert "developer" in out, f"'developer' should be preserved in EN. Got: {out!r}"
    # 'widget' IS banned in EN — replaced by 'website chat'
    assert "widget" not in out, f"'widget' should be replaced in EN. Got: {out!r}"
    # 'hosting' IS banned in EN — replaced by 'server for the bot'
    assert "hosting" not in out, f"'hosting' should be replaced in EN. Got: {out!r}"


# ---------------------------------------------------------------------------
# Test 5: Accept-Language header detection
# ---------------------------------------------------------------------------

def test_accept_language_detection(client):
    """POST /api/session with different Accept-Language headers sets language correctly."""
    # English priority
    r_en = client.post(
        "/api/session",
        json={},
        headers={"Accept-Language": "en-US,en;q=0.9,ru;q=0.3"},
    )
    assert r_en.status_code == 200
    assert r_en.json()["language"] == "en", (
        f"Expected 'en' for en-US header. Got: {r_en.json()['language']!r}"
    )

    # Russian priority (or tie defaults to RU)
    r_ru = client.post(
        "/api/session",
        json={},
        headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.3"},
    )
    assert r_ru.status_code == 200
    assert r_ru.json()["language"] == "ru", (
        f"Expected 'ru' for ru-RU header. Got: {r_ru.json()['language']!r}"
    )

    # Tie (equal weights) defaults to RU
    r_tie = client.post(
        "/api/session",
        json={},
        headers={"Accept-Language": "en;q=0.5,ru;q=0.5"},
    )
    assert r_tie.status_code == 200
    assert r_tie.json()["language"] == "ru", (
        f"Expected 'ru' for tie weights. Got: {r_tie.json()['language']!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: summary template renders EN with USD
# ---------------------------------------------------------------------------

def test_summary_template_renders_en_with_usd():
    """Render SUMMARY_TEMPLATE in 'en' with a populated WorkflowState.
    Assert: English headers, USD budget display, no Cyrillic in section headers."""
    from app.state import Phase, WorkflowState
    from app.workflows.common_prompts import SUMMARY_TEMPLATE

    state = WorkflowState()
    state.conversation.language = "en"
    state.point_a.business_type = "online crafts store"
    state.point_a.channels = ["Instagram", "Website"]
    state.point_a.tools_in_use = ["Excel"]
    state.point_a.pain_points = ["orders slip through"]
    state.point_b.desired_outcome = "zero missed orders"
    state.point_b.success_metric = "all orders tracked in CRM"
    state.point_b.out_of_scope = ["no mobile app"]
    state.resources.monthly_budget_rub = 300
    state.resources.maintainer = "owner"

    result = SUMMARY_TEMPLATE.render(state=state, phase=Phase.SPEC_REVIEW, lang="en")

    # English section headers
    assert "Let's confirm:" in result, f"Missing EN opener. Got: {result[:200]!r}"
    assert "**Now:**" in result, f"Missing '**Now:**'. Got: {result[:300]!r}"

    # Budget rendered as USD (since lang='en')
    assert "$300" in result or "~$300" in result, (
        f"Expected USD budget ~$300. Got: {result!r}"
    )

    # No Cyrillic in main section headers (brand names in data are OK)
    import re
    # Check that the section-level headers (lines starting with **) are in English
    header_lines = [ln for ln in result.splitlines() if ln.strip().startswith("**")]
    for line in header_lines:
        cyrillic = re.search(r"[А-ЯЁа-яё]{3,}", line)
        assert cyrillic is None, (
            f"Cyrillic found in EN summary header: {line!r}"
        )
