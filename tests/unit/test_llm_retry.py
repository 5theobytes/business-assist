"""Tests for app/llm.py retry logic and backend selection."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, call

import pytest

from app.llm import (
    ANTHROPIC_BASE_BACKOFF_S,
    ANTHROPIC_MAX_RETRIES,
    _call_with_retry,
    _call_gemini_with_retry,
)


class TestCallWithRetry:
    """Test the Anthropic retry wrapper."""

    def test_success_on_first_try(self):
        client = MagicMock()
        client.messages.create.return_value = "result"
        result = _call_with_retry(client, {"model": "test"})
        assert result == "result"
        assert client.messages.create.call_count == 1

    @patch("app.llm.time.sleep")
    def test_retries_on_rate_limit(self, mock_sleep):
        from anthropic import RateLimitError

        client = MagicMock()
        # Fail twice, succeed on third
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        err = RateLimitError.__new__(RateLimitError)
        err.status_code = 429
        err.response = mock_response
        err.body = None
        err.message = "rate limit"
        client.messages.create.side_effect = [err, err, "ok"]

        result = _call_with_retry(client, {"model": "test"})
        assert result == "ok"
        assert client.messages.create.call_count == 3
        assert mock_sleep.call_count == 2

    @patch("app.llm.time.sleep")
    def test_retries_on_server_error(self, mock_sleep):
        from anthropic import APIStatusError

        client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}
        err = APIStatusError.__new__(APIStatusError)
        err.status_code = 500
        err.response = mock_response
        err.body = None
        err.message = "server error"
        client.messages.create.side_effect = [err, "ok"]

        result = _call_with_retry(client, {"model": "test"})
        assert result == "ok"
        assert client.messages.create.call_count == 2

    @patch("app.llm.time.sleep")
    def test_raises_on_client_error(self, mock_sleep):
        from anthropic import APIStatusError

        client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.headers = {}
        err = APIStatusError.__new__(APIStatusError)
        err.status_code = 400
        err.response = mock_response
        err.body = None
        err.message = "bad request"
        client.messages.create.side_effect = err

        with pytest.raises(APIStatusError):
            _call_with_retry(client, {"model": "test"})
        # Should not retry on 4xx
        assert client.messages.create.call_count == 1
        assert mock_sleep.call_count == 0

    @patch("app.llm.time.sleep")
    def test_retries_on_timeout(self, mock_sleep):
        from anthropic import APITimeoutError

        client = MagicMock()
        mock_request = MagicMock()
        err = APITimeoutError.__new__(APITimeoutError)
        err.request = mock_request
        client.messages.create.side_effect = [err, "ok"]

        result = _call_with_retry(client, {"model": "test"})
        assert result == "ok"
        assert client.messages.create.call_count == 2

    @patch("app.llm.time.sleep")
    def test_exhausts_retries_and_raises(self, mock_sleep):
        from anthropic import RateLimitError

        client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {}
        err = RateLimitError.__new__(RateLimitError)
        err.status_code = 429
        err.response = mock_response
        err.body = None
        err.message = "rate limit"
        client.messages.create.side_effect = err

        with pytest.raises(RateLimitError):
            _call_with_retry(client, {"model": "test"})
        assert client.messages.create.call_count == ANTHROPIC_MAX_RETRIES + 1


class TestCallGeminiWithRetry:
    """Test the Gemini retry wrapper."""

    def test_success_on_first_try(self):
        call_fn = MagicMock(return_value="result")
        result = _call_gemini_with_retry(call_fn)
        assert result == "result"
        assert call_fn.call_count == 1

    @patch("app.llm.time.sleep")
    def test_retries_on_429(self, mock_sleep):
        from google.genai import errors as genai_errors

        call_fn = MagicMock()
        err = genai_errors.ClientError.__new__(genai_errors.ClientError)
        err.code = 429
        err.message = "rate limit"
        call_fn.side_effect = [err, "ok"]

        result = _call_gemini_with_retry(call_fn)
        assert result == "ok"
        assert call_fn.call_count == 2

    @patch("app.llm.time.sleep")
    def test_retries_on_server_error(self, mock_sleep):
        from google.genai import errors as genai_errors

        call_fn = MagicMock()
        err = genai_errors.ServerError.__new__(genai_errors.ServerError)
        err.message = "internal"
        call_fn.side_effect = [err, "ok"]

        result = _call_gemini_with_retry(call_fn)
        assert result == "ok"
        assert call_fn.call_count == 2

    def test_raises_on_non_429_client_error(self):
        from google.genai import errors as genai_errors

        call_fn = MagicMock()
        err = genai_errors.ClientError.__new__(genai_errors.ClientError)
        err.code = 403
        err.message = "forbidden"
        call_fn.side_effect = err

        with pytest.raises(genai_errors.ClientError):
            _call_gemini_with_retry(call_fn)
        assert call_fn.call_count == 1
