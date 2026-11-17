from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import ledger
from app.db import apply_schema, make_pool
from app.main import app
from app.rate_limits import transfer_retry_after
from tests.helpers import balance, count, csrf_from_html, ledger_total, page_login, transfer_form


def user_id(db, name):
    return db.execute("SELECT id FROM users WHERE email = %s", (f"{name}@example.com",)).fetchone()[
        "id"
    ]


def test_transfer_limit_is_shared_across_pools_and_atomic(db, pool):
    alice_id = user_id(db, "alice")
    other_pool = make_pool(pool.conninfo)
    other_pool.open(wait=True, timeout=10)
    try:
        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(
                workers.map(
                    lambda i: transfer_retry_after(pool if i % 2 else other_pool, user_id=alice_id),
                    range(40),
                )
            )
    finally:
        other_pool.close()

    assert results.count(0) == 20
    assert all(1 <= seconds <= 60 for seconds in results if seconds)
    row = db.execute("SELECT * FROM transfer_rate_limits").fetchone()
    assert row["user_id"] == alice_id
    assert row["attempts"] == 21  # Saturated, not an unbounded request log.
    assert count(db, "transfer_rate_limits") == 1
    assert transfer_retry_after(pool, user_id=user_id(db, "bob")) == 0


def test_transfer_limit_expires_without_blocked_attempts_extending_it(db, pool):
    alice_id = user_id(db, "alice")
    assert [transfer_retry_after(pool, user_id=alice_id) for _ in range(20)] == [0] * 20
    started_at = db.execute("SELECT window_started_at FROM transfer_rate_limits").fetchone()[
        "window_started_at"
    ]
    for _ in range(3):
        assert 1 <= transfer_retry_after(pool, user_id=alice_id) <= 60
    assert (
        db.execute("SELECT window_started_at FROM transfer_rate_limits").fetchone()[
            "window_started_at"
        ]
        == started_at
    )

    db.execute(
        "UPDATE transfer_rate_limits "
        "SET window_started_at = statement_timestamp() - interval '60 seconds'"
    )
    assert transfer_retry_after(pool, user_id=alice_id) == 0
    row = db.execute("SELECT * FROM transfer_rate_limits").fetchone()
    assert row["attempts"] == 1
    assert row["window_started_at"] > started_at


def test_reapplying_schema_does_not_reset_transfer_limit(db, pool):
    alice_id = user_id(db, "alice")
    for _ in range(20):
        assert transfer_retry_after(pool, user_id=alice_id) == 0
    apply_schema(db)
    assert 1 <= transfer_retry_after(pool, user_id=alice_id) <= 60


@pytest.mark.parametrize("htmx", [False, True])
def test_ui_limit_counts_failures_replays_and_survives_new_sessions(
    db, client, ids, monkeypatch, htmx
):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    headers = {"HX-Request": "true"} if htmx else {}
    data = transfer_form(ids, csrf)
    # Both a success and its replay count, but move money only once.
    assert client.post("/ui/transfer", data=data, headers=headers).status_code == 200
    replay = client.post("/ui/transfer", data=data, headers=headers)
    assert replay.status_code == 200
    assert "already processed" in replay.text
    for attempt in range(18):
        response = client.post(
            "/ui/transfer",
            data={
                **data,
                "to_email": f"unknown-{attempt}@example.com",
                "idempotency_key": f"probe-{attempt}",
            },
            headers=headers,
        )
        assert response.status_code == 422

    entries_before = count(db, "ledger_entries")
    transfers_before = count(db, "transfers")
    source_before = balance(db, ids["alice"]["Checking"])
    destination_before = balance(db, ids["bob"]["Checking"])

    def unexpected_lookup(*args, **kwargs):
        pytest.fail("Throttled requests must not look up recipients or move money")

    with monkeypatch.context() as patch:
        patch.setattr(ledger, "resolve_recipient_account", unexpected_lookup)
        patch.setattr(ledger, "execute_transfer", unexpected_lookup)
        # A new app lifespan and a fresh login still share the database counter.
        with TestClient(app, base_url="http://localhost") as other:
            assert page_login(other, "alice").status_code == 303
            fresh_csrf = csrf_from_html(other.get("/ui/transfer").text)
            for email in ("bob@example.com", "unknown@example.com", "malformed"):
                blocked = other.post(
                    "/ui/transfer",
                    data={**data, "csrf_token": fresh_csrf, "to_email": email},
                    headers=headers,
                )
                assert blocked.status_code == 429
                assert 1 <= int(blocked.headers["Retry-After"]) <= 60
                assert "Too many transfer attempts. Please try again shortly." in blocked.text
                assert ("<html" not in blocked.text) == htmx
                assert 'name="idempotency_key" value="form-1"' in blocked.text

    assert count(db, "ledger_entries") == entries_before
    assert count(db, "transfers") == transfers_before
    assert balance(db, ids["alice"]["Checking"]) == source_before
    assert balance(db, ids["bob"]["Checking"]) == destination_before
    assert ledger_total(db) == 0


def test_ui_limit_is_per_user_and_csrf_failures_do_not_consume_it(db, client, ids):
    assert page_login(client, "alice").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    for _ in range(21):
        invalid = client.post("/ui/transfer", data=transfer_form(ids, "wrong-csrf"))
        assert invalid.status_code == 403
    assert count(db, "transfer_rate_limits") == 0

    # Invalid amounts and malformed emails count too, before recipient lookup.
    for attempt in range(20):
        data = transfer_form(ids, csrf, to="malformed", amount="abc" if attempt % 2 else "1")
        assert client.post("/ui/transfer", data=data).status_code == 422
    assert client.post("/ui/transfer", data=transfer_form(ids, csrf)).status_code == 429

    assert page_login(client, "bob").status_code == 303
    csrf = csrf_from_html(client.get("/ui/transfer").text)
    data = transfer_form(ids, csrf, to="alice@example.com")
    data["from_account_id"] = str(ids["bob"]["Checking"])
    response = client.post("/ui/transfer", data=data)
    assert response.status_code == 200
    assert "Sent $12.34." in response.text
    assert ledger_total(db) == 0
