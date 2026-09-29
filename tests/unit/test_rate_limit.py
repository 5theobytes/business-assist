"""Tests for app/rate_limit.py — in-memory IP rate limiter."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.rate_limit import _IPBuckets, _client_ip, rate_limit, rate_limiter


class TestIPBuckets:
    def setup_method(self):
        self.buckets = _IPBuckets()

    def test_allows_requests_within_limit(self):
        for _ in range(5):
            assert self.buckets.hit("key1", limit=5, window_s=60) is True

    def test_blocks_after_limit_exceeded(self):
        for _ in range(5):
            self.buckets.hit("key1", limit=5, window_s=60)
        assert self.buckets.hit("key1", limit=5, window_s=60) is False

    def test_different_keys_independent(self):
        for _ in range(5):
            self.buckets.hit("key1", limit=5, window_s=60)
        # key2 should still be allowed
        assert self.buckets.hit("key2", limit=5, window_s=60) is True

    def test_expired_entries_are_evicted(self):
        with patch("app.rate_limit.time.monotonic") as mock_time:
            mock_time.return_value = 100.0
            for _ in range(5):
                self.buckets.hit("key1", limit=5, window_s=60)
            # Advance time past window
            mock_time.return_value = 161.0
            assert self.buckets.hit("key1", limit=5, window_s=60) is True

    def test_reset_clears_all_buckets(self):
        for _ in range(5):
            self.buckets.hit("key1", limit=5, window_s=60)
        self.buckets.reset()
        assert self.buckets.hit("key1", limit=5, window_s=60) is True


class TestClientIP:
    def _make_request(self, headers=None, client_host="127.0.0.1"):
        request = MagicMock()
        request.headers = headers or {}
        request.client = MagicMock()
        request.client.host = client_host
        return request

    def test_uses_cf_connecting_ip_when_present(self):
        req = self._make_request(headers={"cf-connecting-ip": "1.2.3.4"})
        assert _client_ip(req) == "1.2.3.4"

    def test_uses_x_forwarded_for_when_no_cf(self):
        req = self._make_request(headers={"x-forwarded-for": "5.6.7.8, 10.0.0.1"})
        assert _client_ip(req) == "5.6.7.8"

    def test_falls_back_to_client_host(self):
        req = self._make_request(headers={}, client_host="192.168.1.1")
        assert _client_ip(req) == "192.168.1.1"

    def test_returns_unknown_when_no_client(self):
        request = MagicMock()
        request.headers = {}
        request.client = None
        assert _client_ip(request) == "unknown"


class TestRateLimitDependency:
    def setup_method(self):
        rate_limiter.reset()

    def test_allows_within_limit(self):
        dep = rate_limit(limit=3, window_s=60)
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "10.0.0.1"
        request.url.path = "/test"

        # Should not raise
        for _ in range(3):
            dep(request)

    def test_raises_429_when_exceeded(self):
        dep = rate_limit(limit=2, window_s=60)
        request = MagicMock()
        request.headers = {}
        request.client = MagicMock()
        request.client.host = "10.0.0.2"
        request.url.path = "/test"

        dep(request)
        dep(request)
        with pytest.raises(HTTPException) as exc_info:
            dep(request)
        assert exc_info.value.status_code == 429
