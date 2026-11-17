import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from tests.helpers import TEST_WEBHOOK_SECRET

VALID_URLS = [
    "http://127.0.0.1:8101/hooks",
    "https://hooks.example.com/ledgerlite?source=ledger",
    "  HTTPS://Hooks.Example.com:8443/path  ",
]
INVALID_URLS = [
    "ftp://hooks.example.com/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "hooks.example.com/hook",
    "/relative/path",
    "http://",
    "https:///path-only",
    "http://hooks.example.com:99999/x",
    "http://hooks.example.com:port/x",
    "http://hooks example.com/x",
    "http://hooks.example.com/a\nb",
]


@pytest.fixture
def make_settings(monkeypatch):
    """Build Settings from the environment only (no .env), as the app does at startup."""

    def build(**env: str | None) -> Settings:
        for name in ("WEBHOOK_URL", "WEBHOOK_SIGNING_SECRET"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            if value is not None:
                monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return build


def assert_secret_free(*texts: str) -> None:
    for text in texts:
        assert TEST_WEBHOOK_SECRET not in text


def assert_safe_error(error: ValidationError) -> None:
    assert_secret_free(
        str(error), repr(error), error.json(), json.dumps(error.errors(), default=str)
    )


@pytest.mark.parametrize("url", VALID_URLS)
def test_http_url_with_secret_enables_webhooks(make_settings, url):
    settings = make_settings(WEBHOOK_URL=url, WEBHOOK_SIGNING_SECRET=TEST_WEBHOOK_SECRET)

    assert settings.webhooks_enabled is True
    assert settings.webhook_url == url.strip()
    assert settings.webhook_signing_secret.get_secret_value() == TEST_WEBHOOK_SECRET
    assert_secret_free(repr(settings), str(settings), settings.model_dump_json())


@pytest.mark.parametrize("url", [None, "", "   "], ids=["absent", "empty", "blank"])
@pytest.mark.parametrize("secret", [None, "", TEST_WEBHOOK_SECRET], ids=["none", "empty", "set"])
def test_absent_or_empty_url_disables_webhooks_without_a_secret(make_settings, url, secret):
    settings = make_settings(WEBHOOK_URL=url, WEBHOOK_SIGNING_SECRET=secret)

    assert settings.webhooks_enabled is False
    assert settings.webhook_url is None
    assert_secret_free(repr(settings), settings.model_dump_json())


@pytest.mark.parametrize("secret", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_enabled_url_requires_a_signing_secret(make_settings, secret):
    with pytest.raises(ValidationError) as caught:
        make_settings(WEBHOOK_URL="https://hooks.example.com/x", WEBHOOK_SIGNING_SECRET=secret)

    assert "WEBHOOK_SIGNING_SECRET is required" in str(caught.value)
    assert_safe_error(caught.value)


@pytest.mark.parametrize("url", INVALID_URLS)
def test_enabled_url_must_be_absolute_http_or_https(make_settings, url):
    with pytest.raises(ValidationError) as caught:
        make_settings(WEBHOOK_URL=url, WEBHOOK_SIGNING_SECRET=TEST_WEBHOOK_SECRET)

    assert "WEBHOOK_URL must be an absolute http:// or https:// URL" in str(caught.value)
    assert_safe_error(caught.value)


def test_invalid_webhook_config_stops_app_startup_without_leaking_secret(webhook_config):
    from app.main import app

    webhook_config(url="gopher://hooks.example.com/", secret=TEST_WEBHOOK_SECRET)
    with pytest.raises(ValidationError) as caught, TestClient(app, base_url="http://localhost"):
        pass

    assert_safe_error(caught.value)


def test_jwt_secret_errors_do_not_echo_the_value(make_settings, monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "short-marker")
    with pytest.raises(ValidationError) as caught:
        make_settings()

    assert "short-marker" not in str(caught.value)
