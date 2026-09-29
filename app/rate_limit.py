"""Простой in-memory rate-limiter по IP-адресу клиента.

Используем как FastAPI-зависимость:

    @app.post(..., dependencies=[Depends(rate_limit(limit=5, window_s=60))])
    def intake(...):
        ...

Хранилище — process-local dict; на free-tier Render один инстанс, этого хватает.
Если переедем на multi-instance — заменим на Redis-bucket (sliding window).

slowapi не использовали из-за конфликта декоратора с FastAPI body-парсингом
(сигнатура после декорирования не распознаётся как POST body) в текущей
комбинации версий.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request


class _IPBuckets:
    """Скользящее окно по IP+пути. Хранит timestamps последних запросов."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str, *, limit: int, window_s: int) -> bool:
        """True если разрешено, False если лимит превышен."""
        bucket = self._buckets[key]
        now = time.monotonic()
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True

    def reset(self) -> None:
        """Очистка — для тестов."""
        self._buckets.clear()


# Глобальный инстанс. Тесты могут вызвать reset() через rate_limiter._buckets.reset().
rate_limiter = _IPBuckets()


def _client_ip(request: Request) -> str:
    """IP клиента; за прокси Cloudflare/Render — берём X-Forwarded-For/CF-Connecting-IP."""
    xff = request.headers.get("cf-connecting-ip") or request.headers.get(
        "x-forwarded-for", ""
    ).split(",")[0].strip()
    if xff:
        return xff
    return request.client.host if request.client else "unknown"


def rate_limit(*, limit: int, window_s: int = 60) -> Callable:
    """Фабрика FastAPI-зависимостей: возвращает функцию, которую FastAPI вызовет
    с Request, и которая бросит 429 при превышении лимита."""

    def dep(request: Request) -> None:
        ip = _client_ip(request)
        key = f"{request.url.path}|{ip}"
        if not rate_limiter.hit(key, limit=limit, window_s=window_s):
            raise HTTPException(
                status_code=429, detail="слишком много запросов, подождите минуту",
            )

    return dep
