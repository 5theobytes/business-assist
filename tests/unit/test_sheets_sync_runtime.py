"""Tests for app/sheets_sync_runtime — onlайн-debouncer."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Чистый дебаунсер на каждый тест + GOOGLE_SHEETS_ID задан."""
    from app import sheets_sync_runtime
    sheets_sync_runtime.reset_for_tests()
    monkeypatch.setenv("GOOGLE_SHEETS_ID", "test-sheet-id")
    yield
    sheets_sync_runtime.reset_for_tests()


def test_schedule_sync_returns_false_when_disabled(monkeypatch):
    monkeypatch.delenv("GOOGLE_SHEETS_ID", raising=False)
    from app import sheets_sync_runtime
    sheets_sync_runtime.reset_for_tests()
    assert sheets_sync_runtime.schedule_sync(debounce_seconds=0) is False


def test_schedule_sync_returns_true_first_call():
    from app import sheets_sync_runtime
    with patch("scripts.sync_to_sheets.run_full_sync", return_value={"sessions": 0, "sheet_url": None}):
        ok = sheets_sync_runtime.schedule_sync(debounce_seconds=0)
        assert ok is True
        # дать треду завершиться
        time.sleep(0.2)


def test_schedule_sync_debounces_concurrent_calls():
    """Второй call в течение pending окна — no-op (debounce)."""
    from app import sheets_sync_runtime
    with patch("scripts.sync_to_sheets.run_full_sync", return_value={"sessions": 0, "sheet_url": None}):
        first = sheets_sync_runtime.schedule_sync(debounce_seconds=2)
        second = sheets_sync_runtime.schedule_sync(debounce_seconds=2)
        third = sheets_sync_runtime.schedule_sync(debounce_seconds=2)
        assert first is True
        assert second is False
        assert third is False


def test_schedule_sync_calls_run_full_sync_after_debounce():
    """После timeout-а run_full_sync должен быть вызван."""
    from app import sheets_sync_runtime
    with patch("scripts.sync_to_sheets.run_full_sync") as mock_sync:
        mock_sync.return_value = {"sessions": 5, "sheet_url": "https://docs.google.com/..."}
        sheets_sync_runtime.schedule_sync(debounce_seconds=0)
        time.sleep(0.3)  # дать треду отработать
        mock_sync.assert_called_once()


def test_schedule_sync_swallows_run_full_sync_errors():
    """Если sync упал — это НЕ должно валить вызывающий код или флаг pending."""
    from app import sheets_sync_runtime
    with patch("scripts.sync_to_sheets.run_full_sync", side_effect=RuntimeError("boom")):
        ok = sheets_sync_runtime.schedule_sync(debounce_seconds=0)
        assert ok is True
        time.sleep(0.3)
    # После ошибки флаг должен быть сброшен — следующий schedule снова сработает.
    with patch("scripts.sync_to_sheets.run_full_sync", return_value={"sessions": 0, "sheet_url": None}):
        ok = sheets_sync_runtime.schedule_sync(debounce_seconds=0)
        assert ok is True
        time.sleep(0.2)


def test_run_sync_inline_calls_run_full_sync():
    """Inline-эндпоинт обходит дебаунс и сразу зовёт sync."""
    from app import sheets_sync_runtime
    with patch("scripts.sync_to_sheets.run_full_sync") as mock_sync:
        mock_sync.return_value = {"sessions": 3, "sheet_url": "https://..."}
        result = sheets_sync_runtime.run_sync_inline()
        mock_sync.assert_called_once()
        assert result["sessions"] == 3
