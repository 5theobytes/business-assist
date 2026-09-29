"""Opt-in live integration for Google Cloud Speech-to-Text.

Run only with explicit live-test opt-in, Google Cloud credentials, and the
optional local audio fixture. The test may incur provider charges.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import app.bootstrap  # noqa: F401  side-effect

pytestmark = [pytest.mark.integration, pytest.mark.requires_google_speech]

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "voice_test.ogg"


@pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="tests/fixtures/voice_test.ogg missing — record a short Telegram voice with the word 'привет' and place it there",
)
def test_speech_to_text_recognises_short_clip():
    from app.telegram_bot import transcribe

    audio = FIXTURE.read_bytes()
    transcript = transcribe(audio, language_code="ru-RU")

    assert transcript.strip() != ""
    assert "привет" in transcript.lower()
