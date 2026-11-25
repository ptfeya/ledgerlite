import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main
from app.config import Settings, get_settings

GATEWAY_ENV = "PAYMENTS_GATEWAY_API_KEY"


@pytest.fixture
def clean_gateway_config(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper() == GATEWAY_ENV:
            monkeypatch.delenv(name)
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-not-for-production")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_pytest_setup_uses_dummy_even_with_inherited_key_aliases(tmp_path):
    # On case-sensitive platforms, overwriting the first name leaves the later
    # alias in place for Pydantic's case-insensitive environment lookup.
    setup_check = """
import tests.conftest
from app.config import Settings

key = Settings(_env_file=None).payments_gateway_api_key
assert key.get_secret_value() == "test-only-gateway-key"
"""
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        GATEWAY_ENV: "inherited-upper-test-value",
        "PaYmEnTs_GaTeWaY_ApI_KeY": "inherited-mixed-test-value",
    }
    proc = subprocess.run(
        [sys.executable, "-c", setup_check],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    ("value", "message"),
    [(None, "Field required"), ("", "must not be blank"), (" \t ", "must not be blank")],
)
def test_lifespan_rejects_missing_or_blank_key_before_constructing_pool(
    clean_gateway_config, monkeypatch, value, message
):
    if value is not None:
        monkeypatch.setenv(GATEWAY_ENV, value)
    pool = Mock()
    monkeypatch.setattr(main, "make_pool", pool)

    with pytest.raises(ValidationError) as error, TestClient(main.app):
        pytest.fail("Application startup unexpectedly succeeded")

    assert "payments_gateway_api_key" in str(error.value)
    assert message in str(error.value)
    assert "input_value" not in str(error.value)
    pool.assert_not_called()


def test_gateway_key_prefers_and_masks_environment(clean_gateway_config, monkeypatch):
    dotenv_key = "dotenv-only-test-gateway-key"
    env_key = "env-only-test-gateway-key"
    (clean_gateway_config / ".env").write_text(f"{GATEWAY_ENV}={dotenv_key}\n")

    assert Settings().payments_gateway_api_key.get_secret_value() == dotenv_key

    monkeypatch.setenv(GATEWAY_ENV, env_key)
    settings = Settings()
    assert settings.payments_gateway_api_key.get_secret_value() == env_key
    assert env_key not in repr(settings)
    assert env_key not in settings.model_dump_json()

    monkeypatch.setenv(GATEWAY_ENV, "")
    with pytest.raises(ValidationError, match="must not be blank"):
        Settings()


def test_lifespan_opens_and_closes_pool_with_a_key(clean_gateway_config, monkeypatch):
    monkeypatch.setenv(GATEWAY_ENV, "test-only-startup-gateway-key")
    pool = Mock()
    make_pool = Mock(return_value=pool)
    monkeypatch.setattr(main, "make_pool", make_pool)

    with TestClient(main.app):
        make_pool.assert_called_once_with(get_settings().database_url)
        pool.open.assert_called_once_with(wait=True, timeout=10)
        pool.close.assert_not_called()

    pool.close.assert_called_once_with()
