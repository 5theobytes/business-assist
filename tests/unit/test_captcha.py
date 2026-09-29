"""Tests for app/captcha.py — Cloudflare Turnstile verification."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from app import captcha


def test_verify_returns_true_when_disabled(monkeypatch):
    """Если TURNSTILE_SECRET_KEY не задан — verify пропускается, любой токен ок."""
    monkeypatch.delenv("TURNSTILE_SECRET_KEY", raising=False)
    assert captcha.verify_turnstile("any-token") is True
    assert captcha.verify_turnstile(None) is True


def test_verify_returns_false_when_enabled_but_no_token(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "secret")
    assert captcha.verify_turnstile(None) is False
    assert captcha.verify_turnstile("") is False


def test_verify_calls_cloudflare_with_secret_and_token(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "my-secret")

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"success": True}

    with patch("httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.return_value = mock_response
        ok = captcha.verify_turnstile("user-token", remoteip="1.2.3.4")

    assert ok is True
    mock_client.post.assert_called_once()
    call = mock_client.post.call_args
    assert call.args[0] == captcha.VERIFY_URL
    payload = call.kwargs["data"]
    assert payload["secret"] == "my-secret"
    assert payload["response"] == "user-token"
    assert payload["remoteip"] == "1.2.3.4"


def test_verify_returns_false_on_failed_verification(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "my-secret")

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"success": False, "error-codes": ["invalid-input-response"]}

    with patch("httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.return_value = mock_response
        ok = captcha.verify_turnstile("bad-token")

    assert ok is False


def test_verify_returns_false_on_http_error(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "my-secret")

    mock_response = MagicMock(status_code=500, text="server error")

    with patch("httpx.Client") as mock_client_cls:
        mock_client = mock_client_cls.return_value.__enter__.return_value
        mock_client.post.return_value = mock_response
        assert captcha.verify_turnstile("token") is False


def test_verify_returns_false_on_network_exception(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "my-secret")

    with patch("httpx.Client", side_effect=RuntimeError("net down")):
        assert captcha.verify_turnstile("token") is False


def test_site_key_returns_empty_when_unset(monkeypatch):
    monkeypatch.delenv("TURNSTILE_SITE_KEY", raising=False)
    assert captcha.site_key() == ""


def test_site_key_returns_value_when_set(monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "abc123")
    assert captcha.site_key() == "abc123"
