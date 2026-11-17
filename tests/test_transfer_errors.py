from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolTimeout

from app import ledger
from app.main import app
from tests.helpers import balance, count, csrf_from_html, ledger_total, page_login, transfer_form


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError, PoolTimeout])
@pytest.mark.parametrize("stage", ["acquire", "query"])
@pytest.mark.parametrize("operation", ["resolve_recipient_account", "execute_transfer"])
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

    original = getattr(ledger, operation)

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
            patch.setattr(ledger, operation, unavailable_operation)
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
