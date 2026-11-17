from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ledger
from app.auth import CurrentUser, get_page_user, get_pool
from app.routes import pages
from app.security import session_csrf_token


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
def isolated_transfer_client(monkeypatch, recipient_pool):
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
def test_email_route_delegates_money_movement(isolated_transfer_client, recipient_pool, htmx):
    client, data, execute = isolated_transfer_client
    response = client.post(
        "/ui/transfer", data=data, headers={"HX-Request": "true"} if htmx else {}
    )
    assert response.status_code == 200
    assert "Sent $12.34." in response.text
    assert ("<html" not in response.text) == htmx
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
    assert '<p class="error" role="status">Recipient not found.</p>' in response.text
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
    isolated_transfer_client, recipient_pool, htmx, token
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
    assert "Recipient not found." in response.text
    recipient_pool.connection.assert_not_called()
    execute.assert_not_called()
