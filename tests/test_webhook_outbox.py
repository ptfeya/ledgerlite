import json
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from app import ledger, webhooks
from app.ledger import execute_transfer
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import (
    TEST_WEBHOOK_SECRET,
    TEST_WEBHOOK_URL,
    TRANSFER_FAILURE_MESSAGE,
    balance,
    count,
    csrf_from_html,
    dead_letters,
    ledger_total,
    outbox_events,
    page_login,
    post_transfer,
    transfer_form,
)

EVENT_FIELDS = ["event_id", "type", "transfer_id", "amount_cents", "currency", "created_at"]


@pytest.fixture
def outbound_attempts(monkeypatch):
    """Record (and refuse) any Python-level outbound network connection."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args[-1] if args else kwargs)
        raise OSError("outbound connection blocked by test")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return attempts


def user_id(db, name: str) -> int:
    return db.execute("SELECT id FROM users WHERE email = %s", (f"{name}@example.com",)).fetchone()[
        "id"
    ]


def money_and_queue(db, ids) -> dict:
    return {
        "transfers": count(db, "transfers"),
        "entries": count(db, "ledger_entries"),
        "outbox": outbox_events(db),
        "dead": dead_letters(db),
        "balances": {
            f"{owner}-{account}": balance(db, ids[owner][account])
            for owner in ("alice", "bob")
            for account in ("Checking", "Savings")
        },
    }


def assert_single_event_for(db, transfer_id, amount_cents: int, currency: str = "USD") -> dict:
    events = db.execute(
        "SELECT * FROM webhook_outbox WHERE transfer_id = %s", (transfer_id,)
    ).fetchall()
    assert len(events) == 1
    event = events[0]
    transfer = db.execute("SELECT * FROM transfers WHERE id = %s", (transfer_id,)).fetchone()

    payload = json.loads(event["body"])
    assert list(payload) == EVENT_FIELDS
    assert UUID(payload["event_id"]) == event["event_id"]
    assert payload["type"] == "transfer.completed" == event["event_type"]
    assert payload["transfer_id"] == str(transfer_id)
    assert type(payload["amount_cents"]) is int
    assert payload["amount_cents"] == amount_cents == transfer["amount_cents"]
    assert payload["currency"] == currency
    assert payload["created_at"].endswith("Z")
    created_at = datetime.fromisoformat(payload["created_at"])
    assert created_at.utcoffset() == timedelta(0)
    assert created_at == transfer["created_at"]

    assert event["status"] == "pending"
    assert event["attempts"] == 0
    assert event["delivered_at"] is None
    assert event["last_status_code"] is None
    assert event["last_error"] is None
    assert event["next_attempt_at"] <= datetime.now(UTC)
    # Delivery configuration is read at send time, never snapshotted into the queue.
    row_text = json.dumps(event, default=str)
    assert TEST_WEBHOOK_URL not in row_text
    assert TEST_WEBHOOK_SECRET not in row_text
    return event


def test_api_transfer_commits_one_pending_event_without_http(
    db, client, alice, ids, webhook_config, outbound_attempts, caplog
):
    caplog.set_level(logging.DEBUG)
    webhook_config()
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    start_source, start_destination = balance(db, source), balance(db, destination)

    response = post_transfer(client, alice, "hook-1", source, destination, 2_345)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "from_account_id", "to_account_id", "amount_cents", "created_at"}
    assert (body["from_account_id"], body["to_account_id"], body["amount_cents"]) == (
        source,
        destination,
        2_345,
    )
    assert "idempotent-replay" not in response.headers
    transfer_id = UUID(body["id"])
    stored = db.execute("SELECT created_at FROM transfers WHERE id = %s", (transfer_id,)).fetchone()
    assert datetime.fromisoformat(body["created_at"]) == stored["created_at"]

    entries = db.execute(
        "SELECT account_id, amount_cents FROM ledger_entries WHERE transfer_id = %s "
        "ORDER BY amount_cents",
        (transfer_id,),
    ).fetchall()
    assert entries == [
        {"account_id": source, "amount_cents": -2_345},
        {"account_id": destination, "amount_cents": 2_345},
    ]
    assert balance(db, source) == start_source - 2_345
    assert balance(db, destination) == start_destination + 2_345
    assert ledger_total(db) == 0

    event = assert_single_event_for(db, transfer_id, 2_345)
    assert len(outbox_events(db)) == 1
    assert dead_letters(db) == []
    assert outbound_attempts == []
    assert TEST_WEBHOOK_SECRET not in caplog.text

    # The persisted bytes are a compact UTF-8 JSON snapshot and survive a reconnect.
    with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as fresh:
        reread = fresh.execute(
            "SELECT body, convert_to(body, 'UTF8') AS raw FROM webhook_outbox WHERE event_id = %s",
            (event["event_id"],),
        ).fetchone()
    assert reread["body"] == event["body"]
    assert bytes(reread["raw"]) == event["body"].encode("utf-8")
    assert event["body"] == json.dumps(json.loads(event["body"]), separators=(",", ":"))


def test_api_transfer_response_is_unchanged_by_webhooks(db, client, alice, ids, webhook_config):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    disabled = post_transfer(client, alice, "plain", source, destination, 100)
    webhook_config()
    enabled = post_transfer(client, alice, "hooked", source, destination, 100)

    assert disabled.status_code == enabled.status_code == 201
    assert set(disabled.json()) == set(enabled.json())
    assert sorted(disabled.headers) == sorted(enabled.headers)
    assert len(outbox_events(db)) == 1


@pytest.mark.parametrize("htmx", [False, True])
def test_ui_transfer_commits_one_pending_event_without_http(
    db, client, ids, webhook_config, outbound_attempts, htmx
):
    webhook_config()
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    start_source, start_destination = balance(db, source), balance(db, destination)
    assert page_login(client, "alice").status_code == 303
    token = csrf_from_html(client.get("/ui/transfer").text)
    headers = {"HX-Request": "true"} if htmx else {}
    data = transfer_form(ids, token, key="ui-hook")

    response = client.post("/ui/transfer", data=data, headers=headers)

    assert response.status_code == 200
    assert "Sent $12.34." in response.text
    assert ("<html" not in response.text) == htmx
    transfer = db.execute("SELECT id FROM transfers WHERE idempotency_key = 'ui-hook'").fetchone()
    event = assert_single_event_for(db, transfer["id"], 1_234)
    assert count(db, "ledger_entries") == 8 + 2
    assert balance(db, source) == start_source - 1_234
    assert balance(db, destination) == start_destination + 1_234

    replay = client.post("/ui/transfer", data=data, headers=headers)
    assert replay.status_code == 200
    assert "Sent $12.34 (already processed)." in replay.text
    assert outbox_events(db) == [event]
    assert balance(db, source) == start_source - 1_234
    assert outbound_attempts == []


def test_api_business_failures_create_no_event(db, client, alice, ids, webhook_config):
    webhook_config()
    a_check, b_check = ids["alice"]["Checking"], ids["bob"]["Checking"]
    assert post_transfer(client, alice, "existing", a_check, b_check, 100).status_code == 201
    db.execute("UPDATE accounts SET currency = 'EUR' WHERE id = %s", (ids["bob"]["Savings"],))
    before = money_and_queue(db, ids)
    assert len(before["outbox"]) == 1

    cases = [
        ("overdraft", a_check, b_check, balance(db, a_check) + 1, 422),
        ("foreign-source", b_check, a_check, 100, 404),
        ("system-source", ids["system"], a_check, 100, 404),
        ("system-destination", a_check, ids["system"], 100, 404),
        ("currency", a_check, ids["bob"]["Savings"], 100, 422),
        ("existing", a_check, b_check, 200, 422),
    ]
    for key, source, destination, cents, status in cases:
        response = post_transfer(client, alice, key, source, destination, cents)
        assert response.status_code == status, key

    assert money_and_queue(db, ids) == before

    # A failed attempt does not consume its key; the corrected transfer gets one event.
    retry = post_transfer(client, alice, "overdraft", a_check, b_check, 50)
    assert retry.status_code == 201
    assert_single_event_for(db, UUID(retry.json()["id"]), 50)
    assert outbox_events(db)[0] == before["outbox"][0]
    assert len(outbox_events(db)) == 2


@pytest.mark.parametrize("htmx", [False, True])
def test_ui_business_failure_creates_no_event(db, client, ids, webhook_config, htmx):
    webhook_config()
    before = money_and_queue(db, ids)
    assert page_login(client, "alice").status_code == 303
    token = csrf_from_html(client.get("/ui/transfer").text)
    headers = {"HX-Request": "true"} if htmx else {}

    response = client.post(
        "/ui/transfer", data=transfer_form(ids, token, amount="1000.01"), headers=headers
    )

    assert response.status_code == 422
    assert TRANSFER_FAILURE_MESSAGE in response.text
    assert money_and_queue(db, ids) == before


def _transfer(pool, uid: int, ids, key: str = "atomic", cents: int = 4_321):
    return execute_transfer(
        pool,
        user_id=uid,
        idempotency_key=key,
        from_account_id=ids["alice"]["Checking"],
        to_account_id=ids["bob"]["Checking"],
        amount_cents=cents,
    )


@pytest.mark.parametrize("fault", ["invalid_body", "duplicate_event_id"])
def test_event_insert_failure_rolls_back_the_transfer(
    db, pool, ids, webhook_config, monkeypatch, fault
):
    webhook_config()
    uid = user_id(db, "alice")
    existing = _transfer(pool, uid, ids, key="first", cents=10)
    existing_event = outbox_events(db)[0]
    before = money_and_queue(db, ids)

    with monkeypatch.context() as patch:
        if fault == "invalid_body":
            patch.setattr(webhooks, "transfer_completed_body", lambda **_: "not json")
            expected = psycopg.errors.CheckViolation
        else:
            patch.setattr(webhooks, "uuid4", lambda: existing_event["event_id"])
            expected = psycopg.errors.UniqueViolation
        with pytest.raises(expected):
            _transfer(pool, uid, ids)

    assert money_and_queue(db, ids) == before
    assert existing.id == existing_event["transfer_id"]

    retried = _transfer(pool, uid, ids)
    assert not retried.replayed
    assert_single_event_for(db, retried.id, 4_321)


@pytest.mark.parametrize("fault", ["error_after_enqueue", "commit_time_failure"])
def test_failure_after_enqueue_rolls_back_money_and_event(
    db, pool, ids, webhook_config, monkeypatch, fault
):
    webhook_config()
    uid = user_id(db, "alice")
    before = money_and_queue(db, ids)
    original = webhooks.enqueue_transfer_completed
    enqueued = []

    def enqueue_then_fail(conn, **kwargs):
        enqueued.append(original(conn, **kwargs))
        if fault == "error_after_enqueue":
            raise RuntimeError("abort after enqueue")
        # An unbalanced extra entry trips the deferred zero-sum trigger at COMMIT.
        conn.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) VALUES (%s, %s, 1)",
            (kwargs["transfer_id"], ids["bob"]["Checking"]),
        )

    expected = RuntimeError if fault == "error_after_enqueue" else psycopg.errors.CheckViolation
    with monkeypatch.context() as patch:
        patch.setattr(webhooks, "enqueue_transfer_completed", enqueue_then_fail)
        with pytest.raises(expected):
            _transfer(pool, uid, ids)

    assert len(enqueued) == 1
    assert money_and_queue(db, ids) == before
    assert (
        db.execute("SELECT 1 FROM webhook_outbox WHERE event_id = %s", (enqueued[0],)).fetchone()
        is None
    )

    retried = _transfer(pool, uid, ids)
    assert not retried.replayed
    assert_single_event_for(db, retried.id, 4_321)


def test_separate_connection_sees_money_and_event_only_after_commit(
    db, pool, ids, webhook_config, monkeypatch
):
    webhook_config()
    uid = user_id(db, "alice")
    original = webhooks.enqueue_transfer_completed
    observed = {}

    def observe(transfer_id) -> dict:
        with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as observer:
            return observer.execute(
                "SELECT (SELECT COUNT(*) FROM transfers WHERE id = %(id)s) AS transfers, "
                "(SELECT COUNT(*) FROM ledger_entries WHERE transfer_id = %(id)s) AS entries, "
                "(SELECT COUNT(*) FROM webhook_outbox WHERE transfer_id = %(id)s) AS events",
                {"id": transfer_id},
            ).fetchone()

    def enqueue_and_observe(conn, **kwargs):
        event_id = original(conn, **kwargs)
        inside = conn.execute(
            "SELECT COUNT(*) AS n FROM webhook_outbox WHERE event_id = %s", (event_id,)
        ).fetchone()["n"]
        observed["own_transaction"] = inside
        observed["before_commit"] = observe(kwargs["transfer_id"])
        return event_id

    monkeypatch.setattr(webhooks, "enqueue_transfer_completed", enqueue_and_observe)
    result = _transfer(pool, uid, ids)

    assert observed["own_transaction"] == 1
    assert observed["before_commit"] == {"transfers": 0, "entries": 0, "events": 0}
    assert observe(result.id) == {"transfers": 1, "entries": 2, "events": 1}


def test_event_body_is_a_transfer_time_snapshot(db, pool, ids, webhook_config):
    webhook_config()
    alice_id, bob_id = user_id(db, "alice"), user_id(db, "bob")
    eur = {}
    for uid, owner in ((alice_id, "alice"), (bob_id, "bob")):
        eur[owner] = db.execute(
            "INSERT INTO accounts (user_id, name, currency) VALUES (%s, 'Euro', 'EUR') "
            "RETURNING id",
            (uid,),
        ).fetchone()["id"]
    with db.transaction():
        funding = db.execute(
            "INSERT INTO transfers (user_id, idempotency_key, request_hash, from_account_id, "
            "to_account_id, amount_cents) VALUES (NULL, 'eur-funding', 'seed', %s, %s, 5000) "
            "RETURNING id",
            (ids["system"], eur["alice"]),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
            "VALUES (%s, %s, -5000), (%s, %s, 5000)",
            (funding, ids["system"], funding, eur["alice"]),
        )

    result = execute_transfer(
        pool,
        user_id=alice_id,
        idempotency_key="euro",
        from_account_id=eur["alice"],
        to_account_id=eur["bob"],
        amount_cents=1_999,
    )
    event = assert_single_event_for(db, result.id, 1_999, currency="EUR")

    # Later account or configuration changes never rewrite the stored snapshot.
    db.execute("UPDATE accounts SET currency = 'GBP' WHERE id = ANY(%s)", ([*eur.values()],))
    webhook_config(url="https://changed.example.com/hooks", secret="another-test-secret")
    assert outbox_events(db) == [event]


def test_sequential_api_replay_keeps_one_unchanged_event(db, client, alice, ids, webhook_config):
    webhook_config()
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    first = post_transfer(client, alice, "replay", source, destination, 2_500)
    event = assert_single_event_for(db, UUID(first.json()["id"]), 2_500)

    for _ in range(2):
        again = post_transfer(client, alice, "replay", source, destination, 2_500)
        assert again.status_code == 201
        assert again.json() == first.json()
        assert again.headers["idempotent-replay"] == "true"

    assert outbox_events(db) == [event]
    assert count(db, "ledger_entries") == 8 + 2


@pytest.mark.parametrize("state", ["retry_scheduled", "delivered", "dead_letter"])
def test_replay_never_creates_or_resets_an_event_in_any_state(
    db, client, alice, ids, webhook_config, state
):
    webhook_config()
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    first = post_transfer(client, alice, "stateful", source, destination, 700)
    event_id = assert_single_event_for(db, UUID(first.json()["id"]), 700)["event_id"]

    if state == "retry_scheduled":
        db.execute(
            "UPDATE webhook_outbox SET attempts = 3, last_status_code = 503, "
            "next_attempt_at = now() + interval '1 hour', updated_at = now() "
            "WHERE event_id = %s",
            (event_id,),
        )
    elif state == "delivered":
        db.execute(
            "UPDATE webhook_outbox SET status = 'delivered', attempts = 1, "
            "last_status_code = 204, delivered_at = now(), updated_at = now() "
            "WHERE event_id = %s",
            (event_id,),
        )
    else:
        with db.transaction():
            db.execute(
                "INSERT INTO webhook_dead_letter (event_id, event_type, transfer_id, body, "
                "attempts, last_status_code, created_at) "
                "SELECT event_id, event_type, transfer_id, body, 1, 410, created_at "
                "FROM webhook_outbox WHERE event_id = %s",
                (event_id,),
            )
            db.execute("DELETE FROM webhook_outbox WHERE event_id = %s", (event_id,))
    before = money_and_queue(db, ids)

    api_replay = post_transfer(client, alice, "stateful", source, destination, 700)
    assert api_replay.status_code == 201
    assert api_replay.json() == first.json()
    assert api_replay.headers["idempotent-replay"] == "true"
    assert money_and_queue(db, ids) == before


def test_concurrent_replays_create_one_event(db, pool, ids, webhook_config):
    webhook_config()
    uid = user_id(db, "alice")

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda _: _transfer(pool, uid, ids, key="race"), range(10)))

    assert len({result.id for result in results}) == 1
    assert sum(not result.replayed for result in results) == 1
    assert_single_event_for(db, results[0].id, 4_321)
    assert len(outbox_events(db)) == 1
    assert ledger_total(db) == 0


def test_concurrent_conflicting_keys_create_one_event(db, pool, ids, webhook_config):
    webhook_config()
    uid = user_id(db, "alice")

    def attempt(i: int):
        try:
            return _transfer(pool, uid, ids, key="contested", cents=100 + i % 2)
        except ledger.IdempotencyConflict:
            return None

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = [r for r in executor.map(attempt, range(10)) if r is not None]

    assert len({result.id for result in results}) == 1
    events = outbox_events(db)
    assert len(events) == 1
    assert json.loads(events[0]["body"])["amount_cents"] == results[0].amount_cents


def test_replay_of_transfer_made_while_disabled_does_not_backfill(
    db, client, alice, ids, webhook_config
):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    webhook_config(url="", secret="")
    first = post_transfer(client, alice, "before-enable", source, destination, 300)
    assert first.status_code == 201

    webhook_config()
    replay = post_transfer(client, alice, "before-enable", source, destination, 300)

    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replay"] == "true"
    assert outbox_events(db) == []


@pytest.mark.parametrize("url", [None, "", "   "], ids=["absent", "empty", "blank"])
def test_disabled_webhooks_skip_enqueue_and_keep_backlog(
    db, client, alice, ids, webhook_config, monkeypatch, tmp_path, url
):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    webhook_config()
    assert post_transfer(client, alice, "backlog", source, destination, 100).status_code == 201
    backlog = outbox_events(db)
    assert len(backlog) == 1

    if url is None:
        monkeypatch.chdir(tmp_path)  # no .env to fall back on either
    webhook_config(url=url, secret=None)
    api = post_transfer(client, alice, "while-disabled", source, destination, 200)
    assert api.status_code == 201
    assert page_login(client, "alice").status_code == 303
    token = csrf_from_html(client.get("/ui/transfer").text)
    ui = client.post("/ui/transfer", data=transfer_form(ids, token, key="ui-disabled"))
    assert ui.status_code == 200
    assert "Sent $12.34." in ui.text
    assert outbox_events(db) == backlog

    # Re-enabling (a restart with new settings) affects only new transfers.
    webhook_config()
    after = post_transfer(client, alice, "after-enable", source, destination, 400)
    assert after.status_code == 201
    events = outbox_events(db)
    assert len(events) == 2
    assert events[0] == backlog[0]
    assert_single_event_for(db, UUID(after.json()["id"]), 400)
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM webhook_outbox o JOIN transfers t ON t.id = o.transfer_id "
            "WHERE t.idempotency_key IN ('while-disabled', 'ui-disabled')"
        ).fetchone()["n"]
        == 0
    )
