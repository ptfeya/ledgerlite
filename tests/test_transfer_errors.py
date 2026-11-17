from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from app import ledger
from app.main import app
from app.routes import pages
from tests.helpers import (
    TRANSFER_FAILURE_MESSAGE,
    balance,
    count,
    csrf_from_html,
    ledger_total,
    page_login,
    transfer_form,
)


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError, PoolTimeout])
@pytest.mark.parametrize("stage", ["acquire", "query"])
@pytest.mark.parametrize(
    "operation", ["transfer_retry_after", "resolve_recipient_account", "execute_transfer"]
)
def test_transfer_database_failure_is_safe_and_retryable(
    db, ids, monkeypatch, htmx, error_type, stage, operation
):
    failure_message = "private database connection details"

    class UnavailableConnection:
        def execute(self, *args, **kwargs):
            raise error_type(failure_message)

    @contextmanager
    def unavailable_connection():
        if stage == "acquire":
            raise error_type(failure_message)
        yield UnavailableConnection()

    module = pages if operation == "transfer_retry_after" else ledger
    original = getattr(module, operation)

    def unavailable_operation(pool, **kwargs):
        # Fail inside the real lookup/transfer, after session authentication.
        with monkeypatch.context() as patch:
            patch.setattr(pool, "connection", unavailable_connection)
            return original(pool, **kwargs)

    with TestClient(app, base_url="http://localhost", raise_server_exceptions=False) as client:
        assert page_login(client, "alice").status_code == 303
        csrf = csrf_from_html(client.get("/ui/transfer").text)
        data = transfer_form(ids, csrf)
        headers = {"HX-Request": "true"} if htmx else {}
        entries_before = count(db, "ledger_entries")
        transfers_before = count(db, "transfers")
        source_before = balance(db, ids["alice"]["Checking"])
        destination_before = balance(db, ids["bob"]["Checking"])

        with monkeypatch.context() as patch:
            patch.setattr(module, operation, unavailable_operation)
            response = client.post("/ui/transfer", data=data, headers=headers)

        assert response.status_code == 503
        assert response.json() == {"detail": "Service temporarily unavailable"}
        assert failure_message not in response.text
        assert count(db, "ledger_entries") == entries_before
        assert count(db, "transfers") == transfers_before
        assert balance(db, ids["alice"]["Checking"]) == source_before
        assert balance(db, ids["bob"]["Checking"]) == destination_before

        retry = client.post("/ui/transfer", data=data, headers=headers)
        assert retry.status_code == 200
        assert "Sent $12.34." in retry.text
        assert count(db, "ledger_entries") == entries_before + 2
        assert count(db, "transfers") == transfers_before + 1
        assert balance(db, ids["alice"]["Checking"]) == source_before - 1_234
        assert balance(db, ids["bob"]["Checking"]) == destination_before + 1_234
        assert ledger_total(db) == 0


@pytest.mark.parametrize("htmx", [False, True])
def test_unknown_recipient_and_overdraft_are_indistinguishable(db, client, ids, htmx):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    data = transfer_form(ids, csrf, amount="1000.01")
    headers = {"HX-Request": "true"} if htmx else {}
    entries_before = count(db, "ledger_entries")
    transfers_before = count(db, "transfers")
    known = client.post("/ui/transfer", data=data, headers=headers)
    unknown = client.post(
        "/ui/transfer", data={**data, "to_email": "unknown@example.com"}, headers=headers
    )

    assert known.status_code == unknown.status_code == 422
    assert known.content == unknown.content
    assert known.headers == unknown.headers
    assert f'<p class="error" role="status">{TRANSFER_FAILURE_MESSAGE}</p>' in known.text
    assert ("<html" not in known.text) == htmx
    assert count(db, "ledger_entries") == entries_before
    assert count(db, "transfers") == transfers_before
    assert balance(db, ids["alice"]["Checking"]) == 100_000
    assert balance(db, ids["bob"]["Checking"]) == 75_000
    assert ledger_total(db) == 0

    # Neither failure consumes the key. A corrected transfer can still succeed.
    retry = client.post("/ui/transfer", data={**data, "amount": "12.34"}, headers=headers)
    assert retry.status_code == 200
    assert "Sent $12.34." in retry.text
    assert count(db, "ledger_entries") == entries_before + 2
    assert count(db, "transfers") == transfers_before + 1
    assert ledger_total(db) == 0
