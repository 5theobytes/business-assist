"""Cloudflare Turnstile verification (server-side).

В .env должны быть `TURNSTILE_SITE_KEY` (фронт) и `TURNSTILE_SECRET_KEY` (бэк).
Если оба пусты — verify пропускается (для локальной разработки и unit-тестов).
"""
from __future__ import annotations

import logging
import os

import httpx

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

log = logging.getLogger("captcha")


def is_enabled() -> bool:
    return bool(os.environ.get("TURNSTILE_SECRET_KEY"))


def site_key() -> str:
    """Public key for the front-end widget. Returns '' if not configured."""
    return os.environ.get("TURNSTILE_SITE_KEY") or ""


def verify_turnstile(token: str | None, *, remoteip: str | None = None) -> bool:
    """Returns True если token валидный (или Turnstile отключён). False если проверка не прошла."""
    if not is_enabled():
        # В dev / тестах ключи не заданы — пропускаем как успех.
        return True
    if not token:
        return False
    secret = os.environ["TURNSTILE_SECRET_KEY"]
    payload = {"secret": secret, "response": token}
    if remoteip:
        payload["remoteip"] = remoteip
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.post(VERIFY_URL, data=payload)
            if r.status_code != 200:
                log.warning("turnstile siteverify HTTP %s: %s", r.status_code, r.text)
                return False
            data = r.json()
            return bool(data.get("success"))
    except Exception:
        log.exception("turnstile siteverify failed")
        return False
