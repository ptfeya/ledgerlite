from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from app import ledger
from app.auth import CurrentUser, get_page_user, get_pool
from app.routes import pages
from app.security import session_csrf_token
from tests.helpers import TRANSFER_FAILURE_MESSAGE


@pytest.fixture
def recipient_pool():
    pool = MagicMock()
    conn = pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [{"id": 4, "user_id": 2}]
    return pool


def test_lookup_normalizes_email(recipient_pool):
    result = ledger.resolve_recipient_account(
        recipient_pool, user_id=1, email=" \tBOB@EXAMPLE.COM \r\n"
    )
    assert result == 4
    conn = recipient_pool.connection.return_value.__enter__.return_value
    assert conn.execute.call_args.args[1] == ("bob@example.com",)


@pytest.mark.parametrize(
    "rows",
    [[], [{"id": 4, "user_id": 2}, {"id": 6, "user_id": 2}]],
)
def test_lookup_rejects_missing_or_ambiguous_accounts(recipient_pool, rows):
    conn = recipient_pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = rows
    with pytest.raises(ledger.InvalidTransfer, match="^recipient not found$"):
        ledger.resolve_recipient_account(recipient_pool, user_id=1, email="bob@example.com")


@pytest.mark.parametrize(
    "email",
    ["", " ", "4", "bob", "bob@", "@example.com", "bob@@example.com", "bob @example.com"],
)
def test_lookup_rejects_malformed_emails(recipient_pool, email):
    with pytest.raises(ledger.InvalidTransfer, match="^recipient not found$"):
        ledger.resolve_recipient_account(recipient_pool, user_id=1, email=email)
    recipient_pool.connection.assert_not_called()


def test_lookup_rejects_self(recipient_pool):
    conn = recipient_pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [{"id": 2, "user_id": 1}]
    with pytest.raises(ledger.InvalidTransfer, match="^cannot send to yourself$"):
        ledger.resolve_recipient_account(recipient_pool, user_id=1, email="alice@example.com")


@pytest.fixture
def transfer_throttle(monkeypatch):
    throttle = Mock(return_value=0)
    monkeypatch.setattr(pages, "transfer_retry_after", throttle)
    return throttle


@pytest.fixture
def isolated_transfer_client(monkeypatch, recipient_pool, transfer_throttle):
    # Exercise the real HTML route without a database or the app's pool lifespan.
    app = FastAPI()
    app.include_router(pages.router)
    user = CurrentUser(id=1, email="alice@example.com", token="session", is_admin=False)
    app.dependency_overrides[get_page_user] = lambda: user
    app.dependency_overrides[get_pool] = lambda: recipient_pool
    secret = "test-only-secret-not-for-production"
    monkeypatch.setattr(pages, "get_settings", lambda: SimpleNamespace(jwt_secret=secret))
    monkeypatch.setattr(ledger, "list_accounts", lambda *args: [])
    execute = Mock(return_value=SimpleNamespace(amount_cents=1_234, replayed=False))
    monkeypatch.setattr(ledger, "execute_transfer", execute)
    data = {
        "from_account_id": "2",
        "to_email": " BOB@EXAMPLE.COM ",
        "amount": "12.34",
        "idempotency_key": "form-1",
        "csrf_token": session_csrf_token(secret, user.token),
    }
    with TestClient(app) as client:
        yield client, data, execute


@pytest.mark.parametrize("htmx", [False, True])
def test_email_route_delegates_money_movement(
    isolated_transfer_client, recipient_pool, transfer_throttle, htmx
):
    client, data, execute = isolated_transfer_client
    response = client.post(
        "/ui/transfer", data=data, headers={"HX-Request": "true"} if htmx else {}
    )
    assert response.status_code == 200
    assert "Sent $12.34." in response.text
    assert ("<html" not in response.text) == htmx
    transfer_throttle.assert_called_once_with(recipient_pool, user_id=1)
    execute.assert_called_once_with(
        recipient_pool,
        user_id=1,
        idempotency_key="form-1",
        from_account_id=2,
        to_account_id=4,
        amount_cents=1_234,
    )


@pytest.mark.parametrize("htmx", [False, True])
def test_email_route_renders_generic_error(isolated_transfer_client, recipient_pool, htmx):
    client, data, execute = isolated_transfer_client
    conn = recipient_pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = []
    response = client.post(
        "/ui/transfer", data=data, headers={"HX-Request": "true"} if htmx else {}
    )
    assert response.status_code == 422
    assert f'<p class="error" role="status">{TRANSFER_FAILURE_MESSAGE}</p>' in response.text
    assert ("<html" not in response.text) == htmx
    execute.assert_not_called()


def test_isolated_form_uses_recipient_email(isolated_transfer_client):
    client, _, _ = isolated_transfer_client
    response = client.get("/ui/transfer")
    assert response.status_code == 200
    assert 'for="to_email">To (recipient email)</label>' in response.text
    assert '<input id="to_email" name="to_email" type="email"' in response.text
    assert "to_account_id" not in response.text


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("token", [None, "", "not-the-token"])
def test_isolated_email_transfer_requires_csrf(
    isolated_transfer_client, recipient_pool, transfer_throttle, htmx, token
):
    client, data, execute = isolated_transfer_client
    if token is None:
        del data["csrf_token"]
    else:
        data["csrf_token"] = token
    response = client.post(
        "/ui/transfer", data=data, headers={"HX-Request": "true"} if htmx else {}
    )
    assert response.status_code == 403
    transfer_throttle.assert_not_called()
    recipient_pool.connection.assert_not_called()
    execute.assert_not_called()


def test_isolated_email_transfer_rejects_legacy_destination(
    isolated_transfer_client, recipient_pool
):
    client, data, execute = isolated_transfer_client
    del data["to_email"]
    data["to_account_id"] = "4"
    response = client.post("/ui/transfer", data=data)
    assert response.status_code == 422
    assert TRANSFER_FAILURE_MESSAGE in response.text
    recipient_pool.connection.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize(
    "error",
    [
        ledger.InsufficientFunds("insufficient funds"),
        ledger.InvalidTransfer("currency mismatch"),
        ledger.InvalidTransfer("cannot send to yourself"),
        ledger.AccountNotFound("source account not found"),
        ledger.AccountNotFound("destination account not found"),
        ledger.IdempotencyConflict("Idempotency-Key was already used with a different request"),
    ],
)
def test_business_errors_match_unknown_recipient(
    isolated_transfer_client, recipient_pool, htmx, error
):
    client, data, execute = isolated_transfer_client
    headers = {"HX-Request": "true"} if htmx else {}
    execute.side_effect = error
    known = client.post("/ui/transfer", data=data, headers=headers)
    execute.assert_called_once()
    conn = recipient_pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = []
    unknown = client.post(
        "/ui/transfer", data={**data, "to_email": "unknown@example.com"}, headers=headers
    )
    assert known.status_code == unknown.status_code == 422
    assert known.content == unknown.content
    assert known.headers == unknown.headers
    assert f'<p class="error" role="status">{TRANSFER_FAILURE_MESSAGE}</p>' in known.text
    execute.assert_called_once()


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("email", ["bob@example.com", "unknown@example.com", "malformed", ""])
def test_throttle_precedes_recipient_lookup(
    isolated_transfer_client, recipient_pool, transfer_throttle, htmx, email
):
    client, data, execute = isolated_transfer_client
    transfer_throttle.return_value = 42
    response = client.post(
        "/ui/transfer",
        data={**data, "to_email": email},
        headers={"HX-Request": "true"} if htmx else {},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert "Too many transfer attempts. Please try again shortly." in response.text
    assert ("<html" not in response.text) == htmx
    assert "form-1" in response.text
    transfer_throttle.assert_called_once_with(recipient_pool, user_id=1)
    recipient_pool.connection.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError, PoolTimeout])
def test_throttle_database_failure_is_safe(
    isolated_transfer_client, recipient_pool, transfer_throttle, htmx, error_type
):
    client, data, execute = isolated_transfer_client
    transfer_throttle.side_effect = error_type("private database details")
    response = client.post(
        "/ui/transfer", data=data, headers={"HX-Request": "true"} if htmx else {}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert "private database details" not in response.text
    recipient_pool.connection.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("from_account_id", "bad", "Choose a valid source account."),
        ("amount", "bad", "Enter a positive amount with at most two decimals."),
        ("idempotency_key", "", "Missing idempotency key. Reload the form."),
    ],
)
def test_invalid_forms_count_without_looking_up_recipients(
    isolated_transfer_client, recipient_pool, transfer_throttle, field, value, message
):
    client, data, execute = isolated_transfer_client
    response = client.post("/ui/transfer", data={**data, field: value})
    assert response.status_code == 422
    assert message in response.text
    transfer_throttle.assert_called_once_with(recipient_pool, user_id=1)
    recipient_pool.connection.assert_not_called()
    execute.assert_not_called()
