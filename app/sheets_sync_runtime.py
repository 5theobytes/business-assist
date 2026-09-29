"""Runtime-дебаунсер для онлайн-sync Firestore → Google Sheets.

Идея: после каждого `agent.reply()` (и после `create_session()`) мы вызываем
`schedule_sync()`. Если sync уже запланирован — no-op (debounce). Иначе —
запускаем фоновый поток, который через `SYNC_DEBOUNCE_SECONDS` (по умолчанию
30с) запускает полный sync всех сессий в Sheets.

Несколько подряд идущих сохранений в течение окна → один sync. После окончания
sync — флаг сбрасывается, следующее сохранение запустит новый цикл.

Если `GOOGLE_SHEETS_ID` не задан — schedule_sync no-op'ит молча. Это значит,
что в dev/тестах без env онлайн-sync не запустится и ничего не сломает.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("sheets_sync_runtime")

SYNC_DEBOUNCE_SECONDS = int(os.environ.get("SYNC_DEBOUNCE_SECONDS", "30"))

_state_lock = threading.Lock()
_sync_pending = False


def is_enabled() -> bool:
    """True, если в env задан GOOGLE_SHEETS_ID."""
    return bool(os.environ.get("GOOGLE_SHEETS_ID"))


def reset_for_tests() -> None:
    """Сбросить состояние дебаунсера. Тесты вызывают перед/после."""
    global _sync_pending
    with _state_lock:
        _sync_pending = False


def schedule_sync(*, debounce_seconds: int | None = None) -> bool:
    """Запланировать фоновый sync через debounce_seconds.

    Возвращает True если задача поставлена в эту секунду; False если уже была
    запланирована (debounce-skip) либо sync отключён в env.
    """
    if not is_enabled():
        return False

    global _sync_pending
    with _state_lock:
        if _sync_pending:
            return False  # уже в очереди — коалесцируем
        _sync_pending = True

    delay = debounce_seconds if debounce_seconds is not None else SYNC_DEBOUNCE_SECONDS

    def worker():
        global _sync_pending
        try:
            time.sleep(delay)
            try:
                # Импорт здесь — чтобы scripts/sync_to_sheets.py не подтягивался
                # при старте app (сэкономим cold start на Render).
                from scripts.sync_to_sheets import run_full_sync
                result = run_full_sync(raise_if_unconfigured=False, verbose=False)
                log.info("sheets sync ok: %s", result)
            except Exception:
                log.exception("sheets sync failed")
        finally:
            with _state_lock:
                _sync_pending = False

    t = threading.Thread(target=worker, daemon=True, name="sheets-sync")
    t.start()
    return True


def run_sync_inline() -> dict:
    """Синхронно запустить sync прямо сейчас, минуя дебаунс. Для admin-эндпоинта."""
    from scripts.sync_to_sheets import run_full_sync
    return run_full_sync(raise_if_unconfigured=False, verbose=False)
