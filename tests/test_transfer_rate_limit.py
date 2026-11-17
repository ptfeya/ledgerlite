from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import ledger, rate_limit
from app.db import apply_schema, make_pool
from app.main import lifespan
from app.routes import pages
from tests.helpers import balance, count, csrf_from_html, ledger_total, page_login, transfer_form


def user_id(db, name="alice"):
    return db.execute("SELECT id FROM users WHERE email = %s", (f"{name}@example.com",)).fetchone()[
        "id"
    ]


@pytest.mark.parametrize("htmx", [False, True])
@pytest.mark.parametrize("recipient", ["unknown@example.com", "bob@example.com"])
def test_transfer_limit_counts_failures_and_blocks_before_lookup(
    db, client, ids, monkeypatch, htmx, recipient
):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    data = transfer_form(ids, csrf, to=recipient, amount="1000.01")
    headers = {"HX-Request": "true"} if htmx else {}
    entries_before = count(db, "ledger_entries")
    transfers_before = count(db, "transfers")
    source_before = balance(db, ids["alice"]["Checking"])
    destination_before = balance(db, ids["bob"]["Checking"])

    for attempt in range(rate_limit.TRANSFER_ATTEMPT_LIMIT):
        # Varying keys must not create a new quota.
        data["idempotency_key"] = f"attempt-{attempt}"
        response = client.post("/ui/transfer", data=data, headers=headers)
        assert response.status_code == 422
        assert "Retry-After" not in response.headers
    window_started_at = db.execute(
        "SELECT window_started_at FROM transfer_rate_limits WHERE user_id = %s", (user_id(db),)
    ).fetchone()["window_started_at"]

    lookup = Mock()
    execute = Mock()
    monkeypatch.setattr(ledger, "resolve_recipient_account", lookup)
    monkeypatch.setattr(ledger, "execute_transfer", execute)
    data["idempotency_key"] = "blocked"
    responses = [
        client.post("/ui/transfer", data={**data, "to_email": email}, headers=headers)
        for email in ("unknown@example.com", "bob@example.com", "alice@example.com", "")
    ]
    for response in responses:
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= rate_limit.TRANSFER_WINDOW_SECONDS
        assert (
            '<p class="error" role="status">Too many transfer attempts. '
            "Please try again shortly.</p>"
        ) in response.text
        assert ("<html" not in response.text) == htmx
        assert response.content == responses[0].content
        if htmx:
            assert 'value="blocked" hx-swap-oob="true"' in response.text
    lookup.assert_not_called()
    execute.assert_not_called()
    counter = db.execute(
        "SELECT attempts, window_started_at FROM transfer_rate_limits WHERE user_id = %s",
        (user_id(db),),
    ).fetchone()
    assert counter["attempts"] == rate_limit.TRANSFER_ATTEMPT_LIMIT + 1
    assert counter["window_started_at"] == window_started_at
    assert count(db, "ledger_entries") == entries_before
    assert count(db, "transfers") == transfers_before
    assert balance(db, ids["alice"]["Checking"]) == source_before
    assert balance(db, ids["bob"]["Checking"]) == destination_before
    assert ledger_total(db) == 0


def test_quota_is_shared_across_sessions_and_workers_but_not_users(db, client, ids):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    data = transfer_form(ids, csrf, to="unknown@example.com")
    for _ in range(rate_limit.TRANSFER_ATTEMPT_LIMIT):
        assert client.post("/ui/transfer", data=data).status_code == 422

    # A separate app and pool model another worker (or a restarted process).
    other_app = FastAPI(lifespan=lifespan)
    other_app.include_router(pages.router)
    with TestClient(other_app, base_url="http://localhost") as other:
        assert page_login(other, "alice").status_code == 303
        new_csrf = csrf_from_html(other.get("/ui/transfer").text)
        assert (
            other.post(
                "/ui/transfer",
                data={**data, "csrf_token": new_csrf},
                headers={"HX-Request": "true"},
            ).status_code
            == 429
        )

        assert page_login(other, "bob").status_code == 303
        bob_csrf = csrf_from_html(other.get("/ui/transfer").text)
        bob_data = {**data, "csrf_token": bob_csrf, "from_account_id": ids["bob"]["Checking"]}
        response = other.post("/ui/transfer", data=bob_data)
        assert response.status_code == 422
        assert "Check the recipient and your balance." in response.text
    assert count(db, "transfer_rate_limits") == 2


def test_successes_and_idempotent_replays_count_toward_limit(db, client, ids):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    data = transfer_form(ids, csrf)
    entries_before = count(db, "ledger_entries")
    for attempt in range(rate_limit.TRANSFER_ATTEMPT_LIMIT):
        response = client.post("/ui/transfer", data=data)
        assert response.status_code == 200
        assert "Sent $12.34" in response.text
        assert ("already processed" in response.text) == (attempt > 0)
    assert client.post("/ui/transfer", data=data).status_code == 429
    assert count(db, "ledger_entries") == entries_before + 2
    assert balance(db, ids["alice"]["Checking"]) == 100_000 - 1_234
    assert balance(db, ids["bob"]["Checking"]) == 75_000 + 1_234
    assert ledger_total(db) == 0


def test_expired_window_allows_a_new_transfer(db, client, ids):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    invalid = transfer_form(ids, csrf, to="unknown@example.com")
    for _ in range(rate_limit.TRANSFER_ATTEMPT_LIMIT):
        assert client.post("/ui/transfer", data=invalid).status_code == 422
    assert client.post("/ui/transfer", data=invalid).status_code == 429
    db.execute(
        "UPDATE transfer_rate_limits SET window_started_at = "
        "statement_timestamp() - INTERVAL '60 seconds' WHERE user_id = %s",
        (user_id(db),),
    )
    response = client.post("/ui/transfer", data=transfer_form(ids, csrf))
    assert response.status_code == 200
    assert "Retry-After" not in response.headers
    assert "Sent $12.34." in response.text
    assert balance(db, ids["alice"]["Checking"]) == 100_000 - 1_234
    assert balance(db, ids["bob"]["Checking"]) == 75_000 + 1_234
    assert (
        db.execute(
            "SELECT attempts FROM transfer_rate_limits WHERE user_id = %s", (user_id(db),)
        ).fetchone()["attempts"]
        == 1
    )
    assert ledger_total(db) == 0


def test_concurrent_attempts_share_one_atomic_counter(db, pool):
    alice_id = user_id(db)
    # Distinct pools ensure correctness does not depend on a process-local lock.
    with make_pool(pool.conninfo) as other_pool, ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(
                rate_limit.transfer_retry_after, pool if attempt % 2 else other_pool, alice_id
            )
            for attempt in range(rate_limit.TRANSFER_ATTEMPT_LIMIT + 8)
        ]
        results = [future.result(timeout=10) for future in futures]
    assert results.count(0) == rate_limit.TRANSFER_ATTEMPT_LIMIT
    assert sum(1 <= retry <= rate_limit.TRANSFER_WINDOW_SECONDS for retry in results) == 8
    assert (
        db.execute(
            "SELECT attempts FROM transfer_rate_limits WHERE user_id = %s", (alice_id,)
        ).fetchone()["attempts"]
        == rate_limit.TRANSFER_ATTEMPT_LIMIT + 1
    )


def test_schema_reapplication_does_not_reset_quota(db, pool):
    alice_id = user_id(db)
    for _ in range(rate_limit.TRANSFER_ATTEMPT_LIMIT):
        assert rate_limit.transfer_retry_after(pool, alice_id) == 0
    apply_schema(db)
    assert rate_limit.transfer_retry_after(pool, alice_id) > 0


def test_invalid_csrf_and_unauthenticated_requests_do_not_consume_quota(db, client, ids):
    data = transfer_form(ids, "invalid-token")
    response = client.post("/ui/transfer", data=data, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert count(db, "transfer_rate_limits") == 0

    assert page_login(client, "alice").status_code == 303
    assert client.post("/ui/transfer", data=data).status_code == 403
    assert count(db, "transfer_rate_limits") == 0
