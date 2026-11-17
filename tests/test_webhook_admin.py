"""Admin dead-letter listing and replay, against the real test database."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from app import webhooks
from app.auth import get_api_admin
from app.ledger import execute_transfer
from app.routes import api
from tests.conftest import TEST_DATABASE_URL, TEST_DBNAME
from tests.helpers import (
    TEST_WEBHOOK_SECRET,
    TEST_WEBHOOK_URL,
    dead_letters,
    outbox_events,
    page_login,
)
from tests.webhook_receiver import Reply

LIST = "/admin/webhooks/dead-letter"
REFUSED_URL = "http://127.0.0.1:9/refused"


def replay_path(event_id) -> str:
    return f"/admin/webhooks/dead-letter/{event_id}/replay"


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def zero(low: float, high: float) -> float:
    return low


@pytest.fixture
def events(db, pool, ids, webhook_config):
    """Commit transfers (and their queued events) through the shared transfer path."""
    webhook_config(url=TEST_WEBHOOK_URL, secret=TEST_WEBHOOK_SECRET)
    alice = db.execute("SELECT id FROM users WHERE email = 'alice@example.com'").fetchone()["id"]
    created = 0

    def make(n: int = 1) -> list[dict]:
        nonlocal created
        made = []
        for _ in range(n):
            created += 1
            result = execute_transfer(
                pool,
                user_id=alice,
                idempotency_key=f"admin-dlq-{created}",
                from_account_id=ids["alice"]["Checking"],
                to_account_id=ids["bob"]["Checking"],
                amount_cents=created,
            )
            made.append(
                db.execute(
                    "SELECT * FROM webhook_outbox WHERE transfer_id = %s", (result.id,)
                ).fetchone()
            )
        return made

    return make


def kill(event_id, *, attempts=6, status=503, error=None, at=None) -> None:
    """Move an outbox event to the dead letter through the worker's own transition."""
    result = webhooks.SendResult(status_code=status, error=error)
    with psycopg.connect(TEST_DATABASE_URL) as conn, conn.cursor() as cur:
        webhooks._move_to_dead_letter(cur, event_id, attempts, result, at or datetime.now(UTC))


@pytest.fixture
def dead(events):
    """Committed dead-letter events, as the worker leaves them."""

    def make(n: int = 1, **kwargs) -> list[dict]:
        made = events(n)
        for event in made:
            kill(event["event_id"], **kwargs)
        return made

    return make


def queue_state(db) -> tuple[list[dict], list[dict]]:
    return outbox_events(db), dead_letters(db)


def money_state(db) -> tuple[list[dict], list[dict], list[dict]]:
    return (
        db.execute("SELECT * FROM transfers ORDER BY id").fetchall(),
        db.execute("SELECT * FROM ledger_entries ORDER BY id").fetchall(),
        db.execute(
            "SELECT account_id, SUM(amount_cents)::bigint AS balance FROM ledger_entries "
            "GROUP BY account_id ORDER BY account_id"
        ).fetchall(),
    )


def outbox_row(db, event_id) -> dict | None:
    return db.execute("SELECT * FROM webhook_outbox WHERE event_id = %s", (event_id,)).fetchone()


# --- authorization -------------------------------------------------------------

ROUTES = [("GET", LIST), ("POST", replay_path("{id}"))]


def test_both_routes_depend_directly_on_get_api_admin():
    found = {}
    for route in api.router.routes:
        if getattr(route, "path", None) == LIST:
            found[("GET", LIST)] = route
        if getattr(route, "path", None) == "/admin/webhooks/dead-letter/{event_id}/replay":
            found[("POST", "replay")] = route
    assert set(found) == {("GET", LIST), ("POST", "replay")}
    for route in found.values():
        assert get_api_admin in [dep.call for dep in route.dependant.dependencies]


def request(client, method, path, event_id, **kwargs):
    return client.request(method, path.replace("{id}", str(event_id)), **kwargs)


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_admin_bearer_is_required(db, client, alice, dead, method, path, monkeypatch):
    [event] = dead()
    before = (queue_state(db), money_state(db))

    def forbidden(*args, **kwargs):
        pytest.fail("rejected callers must not reach the dead-letter queue")

    monkeypatch.setattr("app.routes.api.webhooks.list_dead_letters", forbidden)
    monkeypatch.setattr("app.routes.api.webhooks.replay_dead_letter", forbidden)
    eid = event["event_id"]

    missing = request(client, method, path, eid)
    invalid = request(client, method, path, eid, headers={"Authorization": "Bearer garbage"})
    ordinary = request(client, method, path, eid, headers=alice)
    assert missing.status_code == 401
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert ordinary.status_code == 403

    # A logged-in admin's session cookie is not API authentication.
    assert page_login(client, "admin").status_code == 303
    assert client.cookies.get("ll_session")
    assert request(client, method, path, eid).status_code == 401
    assert request(client, method, path, eid, headers=alice).status_code == 403

    assert (queue_state(db), money_state(db)) == before


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_admin_role_is_rechecked_on_each_request(db, client, admin, dead, method, path):
    first, second = dead(2)
    db.execute("UPDATE users SET is_admin = false WHERE email = 'admin@example.com'")
    before = queue_state(db)

    assert request(client, method, path, first["event_id"], headers=admin).status_code == 403
    assert queue_state(db) == before

    db.execute("UPDATE users SET is_admin = true WHERE email = 'admin@example.com'")
    assert request(client, method, path, second["event_id"], headers=admin).status_code == 200


def test_invalid_event_id_is_rejected_after_authentication(client, admin, alice):
    assert client.post(replay_path("not-a-uuid")).status_code == 401
    assert client.post(replay_path("not-a-uuid"), headers=alice).status_code == 403
    assert client.post(replay_path("not-a-uuid"), headers=admin).status_code == 422


# --- listing -------------------------------------------------------------------


def test_empty_dead_letter_lists_nothing(client, admin):
    response = client.get(LIST, headers=admin)
    assert response.status_code == 200
    assert response.json() == []


def test_list_returns_only_dead_events_with_safe_metadata(db, client, admin, events, dead):
    failed_at = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    [by_status] = dead(status=410, attempts=1, at=failed_at)
    [by_error] = dead(status=None, error="connection_refused", at=failed_at + timedelta(1))
    pending, delivered = events(2)
    db.execute(
        "UPDATE webhook_outbox SET status = 'delivered', attempts = 1, last_status_code = 200 "
        "WHERE event_id = %s",
        (delivered["event_id"],),
    )

    response = client.get(LIST, headers=admin)

    assert response.status_code == 200
    items = response.json()
    assert [item["event_id"] for item in items] == [
        str(by_status["event_id"]),
        str(by_error["event_id"]),
    ]
    assert items[0] == {
        "event_id": str(by_status["event_id"]),
        "event_type": "transfer.completed",
        "transfer_id": str(by_status["transfer_id"]),
        "attempts": 1,
        "last_status_code": 410,
        "last_error": None,
        "created_at": items[0]["created_at"],
        "failed_at": items[0]["failed_at"],
    }
    assert datetime.fromisoformat(items[0]["created_at"]) == by_status["created_at"]
    assert datetime.fromisoformat(items[0]["failed_at"]) == failed_at
    assert (items[1]["attempts"], items[1]["last_status_code"], items[1]["last_error"]) == (
        6,
        None,
        "connection_refused",
    )
    text = response.text
    for marker in (
        TEST_WEBHOOK_SECRET,
        TEST_WEBHOOK_URL,
        "127.0.0.1",
        "v1=",
        "body",
        "amount_cents",
        str(pending["event_id"]),
        str(delivered["event_id"]),
    ):
        assert marker not in text


def test_list_is_bounded_and_limit_cannot_raise_the_bound(client, admin, dead):
    bound = webhooks.DEAD_LETTER_LIST_MAX
    made = dead(bound + 5)
    oldest_first = [str(event["event_id"]) for event in made]

    default = client.get(LIST, headers=admin)
    assert default.status_code == 200
    assert [item["event_id"] for item in default.json()] == oldest_first[:bound]

    small = client.get(LIST, params={"limit": 3}, headers=admin)
    assert [item["event_id"] for item in small.json()] == oldest_first[:3]

    for limit in (bound + 1, 10_000, 0, -1):
        assert client.get(LIST, params={"limit": limit}, headers=admin).status_code == 422


def test_list_helper_clamps_its_limit(pool, dead):
    dead(webhooks.DEAD_LETTER_LIST_MAX + 2)
    assert len(webhooks.list_dead_letters(pool, limit=10_000)) == webhooks.DEAD_LETTER_LIST_MAX
    assert len(webhooks.list_dead_letters(pool, limit=0)) == 1


# --- replay --------------------------------------------------------------------


def test_replay_moves_dead_event_to_one_due_pending_event(db, client, admin, dead):
    [original] = dead()
    [dead_row] = dead_letters(db)
    money_before = money_state(db)

    started = datetime.now(UTC)
    response = client.post(replay_path(original["event_id"]), headers=admin)
    finished = datetime.now(UTC)

    assert response.status_code == 200
    assert dead_letters(db) == []
    [queued] = outbox_events(db)
    assert queued["event_id"] == original["event_id"] == dead_row["event_id"]
    assert queued["event_type"] == original["event_type"]
    assert queued["transfer_id"] == original["transfer_id"]
    assert queued["body"] == original["body"] == dead_row["body"]
    assert queued["created_at"] == original["created_at"] == dead_row["created_at"]
    assert queued["status"] == "pending"
    assert queued["attempts"] == 0
    assert queued["last_status_code"] is None
    assert queued["last_error"] is None
    assert started <= queued["next_attempt_at"] <= finished
    body = response.json()
    assert datetime.fromisoformat(body.pop("next_attempt_at")) == queued["next_attempt_at"]
    assert body == {
        "event_id": str(original["event_id"]),
        "transfer_id": str(original["transfer_id"]),
        "status": "pending",
        "attempts": 0,
    }
    assert money_state(db) == money_before


def test_replay_grants_a_fresh_six_attempt_cycle(db, client, admin, events, receiver, monkeypatch):
    [event] = events()
    clock = Clock(datetime.now(UTC) + timedelta(minutes=1))
    monkeypatch.setattr(webhooks, "utc_now", clock)
    monkeypatch.setattr(webhooks.random, "uniform", zero)
    with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as conn:

        def attempt():
            return webhooks.deliver_next(conn, url=REFUSED_URL, secret=TEST_WEBHOOK_SECRET)

        for _ in range(6):
            result = attempt()
        assert (result.outcome, result.attempt) == ("dead_letter", 6)
        money_before = money_state(db)

        assert client.post(replay_path(event["event_id"]), headers=admin).status_code == 200
        for n in range(1, 6):
            result = attempt()
            assert (result.outcome, result.attempt) == ("retry", n)
            assert outbox_row(db, event["event_id"])["attempts"] == n
        result = attempt()
        assert (result.outcome, result.attempt) == ("dead_letter", 6)
        [again] = dead_letters(db)
        assert (again["attempts"], again["last_error"]) == (6, "connection_refused")
        assert again["body"] == event["body"]

        # A second replay is another fresh cycle, which can now succeed.
        assert client.post(replay_path(event["event_id"]), headers=admin).status_code == 200
        receiver.responder = lambda request: Reply(200)
        result = webhooks.deliver_next(conn, url=receiver.url(), secret=TEST_WEBHOOK_SECRET)
    assert (result.outcome, result.attempt) == ("delivered", 1)
    [request_seen] = receiver.requests
    assert request_seen.body == event["body"].encode()
    assert request_seen.headers["idempotency-key"] == str(event["event_id"])
    assert outbox_row(db, event["event_id"])["status"] == "delivered"
    assert money_state(db) == money_before


def test_replay_of_unknown_pending_or_delivered_event_is_404(db, client, admin, events, dead):
    pending, delivered = events(2)
    dead()
    db.execute(
        "UPDATE webhook_outbox SET status = 'delivered', attempts = 1, last_status_code = 200 "
        "WHERE event_id = %s",
        (delivered["event_id"],),
    )
    before = (queue_state(db), money_state(db))

    for event_id in (uuid4(), pending["event_id"], delivered["event_id"]):
        response = client.post(replay_path(event_id), headers=admin)
        assert response.status_code == 404
        assert response.json() == {"detail": "Dead-letter event not found"}

    assert (queue_state(db), money_state(db)) == before


def test_repeated_replay_does_not_reset_the_requeued_event(db, client, admin, dead):
    [event] = dead()
    assert client.post(replay_path(event["event_id"]), headers=admin).status_code == 200
    db.execute(
        "UPDATE webhook_outbox SET attempts = 3, last_status_code = 503, "
        "next_attempt_at = now() + interval '1 hour' WHERE event_id = %s",
        (event["event_id"],),
    )
    before = (queue_state(db), money_state(db))

    assert client.post(replay_path(event["event_id"]), headers=admin).status_code == 404
    assert (queue_state(db), money_state(db)) == before


def test_replay_failure_rolls_back_to_the_dead_row(db, client, admin, dead, monkeypatch):
    [event] = dead()
    before = (queue_state(db), money_state(db))

    def broken(*args, **kwargs):
        raise RuntimeError("simulated outbox insert failure")

    monkeypatch.setattr(webhooks, "_requeue", broken)
    with pytest.raises(RuntimeError, match="simulated"):
        client.post(replay_path(event["event_id"]), headers=admin)

    assert (queue_state(db), money_state(db)) == before
    monkeypatch.undo()
    assert client.post(replay_path(event["event_id"]), headers=admin).status_code == 200


def test_replay_conflicting_with_queued_event_is_409_without_mutation(db, client, admin, dead):
    [event] = dead()
    # A dead row whose transfer already has an outbox event (never produced by
    # the application itself) must not be duplicated or overwritten.
    blocker_body = event["body"].replace(str(event["event_id"]), str(uuid4()))
    db.execute(
        "INSERT INTO webhook_outbox (event_id, event_type, transfer_id, body) "
        "VALUES (%s, %s, %s, %s)",
        (uuid4(), event["event_type"], event["transfer_id"], blocker_body),
    )
    before = (queue_state(db), money_state(db))

    response = client.post(replay_path(event["event_id"]), headers=admin)

    assert response.status_code == 409
    assert response.json() == {"detail": "An event for this transfer is already queued"}
    assert (queue_state(db), money_state(db)) == before


def _wait_for_lock_waiters(db, n: int, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waiting = db.execute(
            "SELECT COUNT(*) AS n FROM pg_stat_activity WHERE datname = %s "
            "AND wait_event_type = 'Lock' AND query ILIKE '%%webhook_dead_letter%%'",
            (TEST_DBNAME,),
        ).fetchone()["n"]
        if waiting >= n:
            return
        time.sleep(0.02)
    pytest.fail(f"expected {n} replays blocked on the dead-letter row lock")


def test_concurrent_replays_transition_once(db, client, admin, dead):
    [event] = dead()
    money_before = money_state(db)

    # Hold the dead row so both replays are inside their transactions at once.
    with psycopg.connect(TEST_DATABASE_URL) as blocker:
        blocker.execute(
            "SELECT 1 FROM webhook_dead_letter WHERE event_id = %s FOR UPDATE",
            (event["event_id"],),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(client.post, replay_path(event["event_id"]), headers=admin)
                for _ in range(2)
            ]
            _wait_for_lock_waiters(db, 2)
            blocker.rollback()
            responses = [future.result(timeout=10) for future in futures]

    assert sorted(response.status_code for response in responses) == [200, 404]
    winner = next(response for response in responses if response.status_code == 200)
    assert dead_letters(db) == []
    [queued] = outbox_events(db)
    assert queued["event_id"] == event["event_id"]
    assert queued["body"] == event["body"]
    assert queued["attempts"] == 0
    assert datetime.fromisoformat(winner.json()["next_attempt_at"]) == queued["next_attempt_at"]
    assert money_state(db) == money_before


def test_concurrent_replay_helpers_on_separate_connections(db, pool, dead):
    [event] = dead()
    barrier = threading.Barrier(2)
    results = []

    def replay():
        barrier.wait()
        try:
            results.append(webhooks.replay_dead_letter(pool, event["event_id"]))
        except webhooks.ReplayNotFound:
            results.append(None)

    threads = [threading.Thread(target=replay) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 2
    assert results.count(None) == 1
    assert len(outbox_events(db)) == 1
    assert dead_letters(db) == []
