import hashlib
import hmac
import http.client
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg
import pytest
from psycopg.rows import dict_row

from app import webhook_worker, webhooks
from app.config import get_settings
from app.ledger import execute_transfer
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import TEST_WEBHOOK_SECRET, dead_letters, outbox_events
from tests.webhook_receiver import Reply

REFUSED_URL = "http://127.0.0.1:9/refused"
FAST = webhooks.Timeouts(connect=1.0, read=0.5, total=1.5)
NETWORK_ERRORS = {
    "timeout",
    "connection_refused",
    "connection_closed",
    "connection_error",
    "dns_error",
    "tls_error",
    "tls_certificate_invalid",
    "invalid_response",
    "network_error",
}


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def upper(low: float, high: float) -> float:
    return high


def lower(low: float, high: float) -> float:
    return low


def verify(secret: str, header: str, body: bytes) -> bool:
    """Independent receiver-side check of X-LedgerLite-Signature."""
    parts = dict(item.split("=", 1) for item in header.split(","))
    assert set(parts) == {"t", "v1"}
    assert parts["t"].isdigit()
    expected = hmac.new(
        secret.encode(), parts["t"].encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def user_id(db, name: str) -> int:
    return db.execute("SELECT id FROM users WHERE email = %s", (f"{name}@example.com",)).fetchone()[
        "id"
    ]


@pytest.fixture
def queue(db, pool, ids, webhook_config, receiver):
    """Create committed events through the shared transfer path."""
    webhook_config(url=receiver.url(), secret=TEST_WEBHOOK_SECRET)
    alice = user_id(db, "alice")
    created = 0

    def make(n: int = 1) -> list[dict]:
        nonlocal created
        for _ in range(n):
            created += 1
            execute_transfer(
                pool,
                user_id=alice,
                idempotency_key=f"delivery-{created}",
                from_account_id=ids["alice"]["Checking"],
                to_account_id=ids["bob"]["Checking"],
                amount_cents=100 + created,
            )
        return outbox_events(db)

    return make


@pytest.fixture
def worker_conn():
    with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as conn:
        yield conn


def deliver(conn, url, *, clock=None, uniform=upper, secret=TEST_WEBHOOK_SECRET, **kwargs):
    return webhooks.deliver_next(
        conn,
        url=url,
        secret=secret,
        clock=clock or Clock(),
        uniform=uniform,
        **kwargs,
    )


def row(db, event_id) -> dict | None:
    return db.execute("SELECT * FROM webhook_outbox WHERE event_id = %s", (event_id,)).fetchone()


# --- signing -----------------------------------------------------------------


def test_signature_signs_ascii_timestamp_dot_exact_body():
    body = b'{"event_id":"x","amount_cents":1}'
    header = webhooks.signature(TEST_WEBHOOK_SECRET, 1_700_000_000, body)

    assert header.startswith("t=1700000000,v1=")
    digest = header.split("v1=", 1)[1]
    assert digest == digest.lower() and len(digest) == 64
    assert verify(TEST_WEBHOOK_SECRET, header, body)
    assert not verify(TEST_WEBHOOK_SECRET, header, body + b" ")
    assert not verify(TEST_WEBHOOK_SECRET, header.replace("t=1700000000", "t=1700000001"), body)
    assert not verify("another-secret", header, body)


def test_delivery_sends_exact_body_with_signature_and_idempotency_key(
    db, queue, receiver, worker_conn
):
    [event] = queue()
    clock = Clock(datetime(2030, 1, 2, 3, 4, 5, 678_000, tzinfo=UTC))

    result = deliver(worker_conn, receiver.url(), clock=clock)

    assert result.outcome == "delivered" and result.event_id == event["event_id"]
    [request] = receiver.requests
    assert request.method == "POST"
    assert request.path == "/hooks"
    assert request.headers["content-type"] == "application/json"
    assert request.body == event["body"].encode("utf-8")
    assert request.headers["idempotency-key"] == str(event["event_id"])
    assert json.loads(request.body)["event_id"] == str(event["event_id"])
    signature = request.headers["x-ledgerlite-signature"]
    assert signature.startswith(f"t={int(clock.now.timestamp())},v1=")
    assert verify(TEST_WEBHOOK_SECRET, signature, request.body)
    assert not verify(TEST_WEBHOOK_SECRET, signature, request.body.replace(b"1", b"2", 1))

    stored = row(db, event["event_id"])
    assert stored["status"] == "delivered"
    assert stored["attempts"] == 1
    assert stored["delivered_at"] == clock.now
    assert stored["last_status_code"] == 200
    assert stored["last_error"] is None
    assert stored["body"] == event["body"]

    clock.now += timedelta(days=1)
    assert deliver(worker_conn, receiver.url(), clock=clock) is None
    assert len(receiver.requests) == 1


def test_retries_keep_body_and_identity_with_fresh_timestamps(db, queue, receiver, worker_conn):
    [event] = queue()
    receiver.responder = lambda request: Reply(503)
    clock = Clock(datetime(2030, 1, 1, tzinfo=UTC))
    for _ in range(3):
        result = deliver(worker_conn, receiver.url(), clock=clock)
        assert result.outcome == "retry"
        clock.now = row(db, event["event_id"])["next_attempt_at"] + timedelta(seconds=7)

    first, *rest = receiver.requests
    timestamps = []
    for request in receiver.requests:
        assert request.body == first.body == event["body"].encode()
        assert request.headers["idempotency-key"] == str(event["event_id"])
        assert verify(TEST_WEBHOOK_SECRET, request.headers["x-ledgerlite-signature"], request.body)
        timestamps.append(request.headers["x-ledgerlite-signature"].split(",")[0])
    assert len(set(timestamps)) == 3


# --- retry schedule ----------------------------------------------------------


@pytest.mark.parametrize(
    ("attempt", "cap"), [(1, 5), (2, 10), (3, 20), (4, 40), (5, 80), (6, 160), (7, 300), (20, 300)]
)
def test_retry_delay_samples_full_jitter_window(attempt, cap):
    calls = []

    def record(low, high):
        calls.append((low, high))
        return high / 2

    assert webhooks.retry_delay(attempt, uniform=record) == cap / 2
    assert calls == [(0, cap)]
    assert webhooks.retry_delay(attempt, uniform=lower) == 0
    assert webhooks.retry_delay(attempt, uniform=upper) == cap
    for _ in range(200):
        assert 0 <= webhooks.retry_delay(attempt) <= cap


@pytest.mark.parametrize(("pick", "fraction"), [(lower, 0), (upper, 1), (None, 0.25)])
def test_retry_schedule_persists_due_time_and_dead_letters_on_sixth_failure(
    db, queue, receiver, worker_conn, pick, fraction
):
    [event] = queue()
    receiver.responder = lambda request: Reply(500)
    uniform = pick or (lambda low, high: low + (high - low) * fraction)
    clock = Clock(datetime(2030, 1, 1, tzinfo=UTC))

    for attempt, cap in enumerate([5, 10, 20, 40, 80], start=1):
        result = deliver(worker_conn, receiver.url(), clock=clock, uniform=uniform)
        assert (result.outcome, result.attempt, result.status_code) == ("retry", attempt, 500)
        stored = row(db, event["event_id"])
        assert stored["attempts"] == attempt
        assert stored["status"] == "pending"
        assert stored["last_status_code"] == 500
        assert stored["next_attempt_at"] - clock.now == timedelta(seconds=cap * fraction)
        assert len(receiver.requests) == attempt
        if fraction:
            # Not polled before its due time, even by a fresh connection.
            clock.now = stored["next_attempt_at"] - timedelta(microseconds=1)
            with psycopg.connect(TEST_DATABASE_URL) as restarted:
                assert deliver(restarted, receiver.url(), clock=clock) is None
            assert len(receiver.requests) == attempt
        clock.now = stored["next_attempt_at"]

    result = deliver(worker_conn, receiver.url(), clock=clock, uniform=uniform)
    assert (result.outcome, result.attempt) == ("dead_letter", 6)
    assert row(db, event["event_id"]) is None
    [dead] = dead_letters(db)
    assert dead["event_id"] == event["event_id"]
    assert dead["transfer_id"] == event["transfer_id"]
    assert dead["event_type"] == event["event_type"]
    assert dead["body"] == event["body"]
    assert dead["created_at"] == event["created_at"]
    assert dead["attempts"] == 6
    assert dead["last_status_code"] == 500
    assert dead["last_error"] is None
    assert dead["failed_at"] == clock.now

    clock.now += timedelta(days=1)
    assert deliver(worker_conn, receiver.url(), clock=clock) is None
    assert len(receiver.requests) == 6


def test_exhausted_network_failures_dead_letter_with_sanitized_error(db, queue, worker_conn):
    [event] = queue()
    clock = Clock(datetime(2030, 1, 1, tzinfo=UTC))
    for attempt in range(1, 7):
        result = deliver(worker_conn, REFUSED_URL, clock=clock, uniform=lower)
        assert result.attempt == attempt
        assert result.error == "connection_refused"
        assert result.status_code is None
    assert result.outcome == "dead_letter"
    [dead] = dead_letters(db)
    assert dead["event_id"] == event["event_id"]
    assert (dead["attempts"], dead["last_status_code"], dead["last_error"]) == (
        6,
        None,
        "connection_refused",
    )
    assert outbox_events(db) == []


# --- response classes ----------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201, 202, 204, 299])
def test_2xx_marks_delivered(db, queue, receiver, worker_conn, status):
    [event] = queue()
    receiver.responder = lambda request: Reply(status)

    result = deliver(worker_conn, receiver.url())

    assert (result.outcome, result.status_code) == ("delivered", status)
    assert row(db, event["event_id"])["status"] == "delivered"
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 599])
def test_429_and_5xx_schedule_one_retry(db, queue, receiver, worker_conn, status):
    [event] = queue()
    receiver.responder = lambda request: Reply(status, body=b"retry later")

    result = deliver(worker_conn, receiver.url())

    assert (result.outcome, result.status_code, result.attempt) == ("retry", status, 1)
    stored = row(db, event["event_id"])
    assert (stored["status"], stored["attempts"], stored["last_status_code"]) == (
        "pending",
        1,
        status,
    )
    assert len(receiver.requests) == 1
    assert dead_letters(db) == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 408, 409, 410, 413, 422, 451, 499])
def test_other_4xx_dead_letter_after_first_outcome(db, queue, receiver, worker_conn, status):
    [event] = queue()
    receiver.responder = lambda request: Reply(status, body=b"rejected")

    result = deliver(worker_conn, receiver.url())

    assert (result.outcome, result.status_code, result.attempt) == ("dead_letter", status, 1)
    assert row(db, event["event_id"]) is None
    [dead] = dead_letters(db)
    assert (dead["event_id"], dead["attempts"], dead["last_status_code"]) == (
        event["event_id"],
        1,
        status,
    )
    assert dead["body"] == event["body"]
    assert len(receiver.requests) == 1


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
def test_redirects_are_terminal_and_never_followed(db, queue, receiver, worker_conn, status):
    [event] = queue()

    def respond(request):
        if request.path == "/hooks":
            return Reply(status, headers={"Location": receiver.url("/redirect-target")})
        return Reply(200)

    receiver.responder = respond

    result = deliver(worker_conn, receiver.url())

    assert (result.outcome, result.status_code) == ("dead_letter", status)
    time.sleep(0.2)
    assert receiver.paths() == ["/hooks"]
    assert dead_letters(db)[0]["last_status_code"] == status


def test_connection_refused_is_retried(db, queue, worker_conn):
    [event] = queue()

    result = deliver(worker_conn, REFUSED_URL)

    assert (result.outcome, result.error, result.status_code) == (
        "retry",
        "connection_refused",
        None,
    )
    stored = row(db, event["event_id"])
    assert (stored["attempts"], stored["last_error"], stored["last_status_code"]) == (
        1,
        "connection_refused",
        None,
    )


def test_dropped_connection_is_one_retryable_attempt(db, queue, receiver, worker_conn):
    queue()

    def drop(handler):
        handler.close_connection = True
        handler.connection.shutdown(socket.SHUT_RDWR)

    receiver.responder = lambda request: drop

    result = deliver(worker_conn, receiver.url())

    assert (result.outcome, result.error) == ("retry", "connection_closed")
    time.sleep(0.2)
    assert len(receiver.requests) == 1


def test_stalled_receiver_times_out_within_bound(db, queue, receiver, worker_conn):
    [event] = queue()

    def stall(request):
        receiver.released.wait(10)
        return Reply(200)

    receiver.responder = stall
    started = time.monotonic()

    result = deliver(worker_conn, receiver.url(), timeouts=FAST)

    assert time.monotonic() - started < FAST.total + 1
    assert (result.outcome, result.error) == ("retry", "timeout")
    assert row(db, event["event_id"])["last_error"] == "timeout"


def test_trickling_response_is_cut_off_by_total_deadline(db, queue, receiver, worker_conn):
    queue()

    def trickle(handler):
        handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
        handler.wfile.flush()
        for _ in range(100):
            if receiver.released.wait(0.2):
                return
            try:
                handler.wfile.write(b"X-Slow: 1\r\n")
                handler.wfile.flush()
            except OSError:
                return

    receiver.responder = lambda request: trickle
    started = time.monotonic()

    result = deliver(worker_conn, receiver.url(), timeouts=FAST)

    assert time.monotonic() - started < FAST.total + 1
    assert (result.outcome, result.error) == ("retry", "timeout")


def test_large_response_body_is_not_read(db, queue, receiver, worker_conn):
    queue()

    def huge(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", str(1 << 30))
        handler.end_headers()
        chunk = b"x" * 65536
        for _ in range(1 << 14):
            if receiver.released.is_set():
                return
            try:
                handler.wfile.write(chunk)
            except OSError:
                return

    receiver.responder = lambda request: huge
    started = time.monotonic()

    result = deliver(worker_conn, receiver.url(), timeouts=FAST)

    assert time.monotonic() - started < FAST.total
    assert (result.outcome, result.status_code) == ("delivered", 200)


def test_https_uses_verified_tls(monkeypatch):
    contexts = []
    original = http.client.HTTPSConnection.connect

    def recording_connect(self):
        contexts.append(self._context)
        return original(self)

    monkeypatch.setattr(http.client.HTTPSConnection, "connect", recording_connect)
    result = webhooks.post_webhook("https://127.0.0.1:9/hooks", b"{}", {}, FAST)

    [context] = contexts
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert result.error == "connection_refused"


def test_https_against_plain_http_fails_tls_without_sending_body(db, queue, receiver, worker_conn):
    [event] = queue()

    result = deliver(worker_conn, receiver.url().replace("http://", "https://"), timeouts=FAST)

    assert (result.outcome, result.error) == ("retry", "tls_error")
    assert receiver.requests == []
    assert row(db, event["event_id"])["last_error"] == "tls_error"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("timed out to https://secret.example/x"), "timeout"),
        (ConnectionRefusedError(61, "refused"), "connection_refused"),
        (http.client.RemoteDisconnected("closed"), "connection_closed"),
        (ConnectionResetError(54, "reset"), "connection_error"),
        (socket.gaierror(8, "nodename nor servname provided"), "dns_error"),
        (ssl.SSLCertVerificationError(1, "certificate verify failed"), "tls_certificate_invalid"),
        (ssl.SSLError(1, "wrong version number"), "tls_error"),
        (http.client.LineTooLong("header line"), "invalid_response"),
        (OSError(65, "No route to host"), "network_error"),
    ],
)
def test_transport_errors_map_to_fixed_safe_classifications(exc, expected):
    assert webhooks.classify_error(exc) == expected
    assert expected in NETWORK_ERRORS


def test_unexpected_status_outside_http_range_is_terminal(db, queue, receiver, worker_conn):
    [event] = queue()

    def weird(handler):
        handler.wfile.write(b"HTTP/1.1 799 Odd\r\nContent-Length: 0\r\n\r\n")

    receiver.responder = lambda request: weird

    result = deliver(worker_conn, receiver.url())

    assert (result.outcome, result.status_code, result.error) == (
        "dead_letter",
        None,
        "invalid_status",
    )
    [dead] = dead_letters(db)
    assert (dead["event_id"], dead["last_error"]) == (event["event_id"], "invalid_status")


def test_persisted_errors_and_results_hold_no_secrets_urls_or_responses(
    db, queue, receiver, worker_conn
):
    queue(2)
    marker = "response-marker-do-not-store"
    receiver.responder = lambda request: Reply(
        500, headers={"X-Marker": marker}, body=marker.encode()
    )
    url_marker = "url-marker-do-not-store"
    results = [
        deliver(worker_conn, receiver.url(f"/hooks?token={url_marker}")),
        deliver(worker_conn, f"http://127.0.0.1:9/{url_marker}"),
    ]

    stored = json.dumps([outbox_events(db), [vars(r) for r in results]], default=str)
    signatures = [r.headers["x-ledgerlite-signature"] for r in receiver.requests]
    for needle in [marker, url_marker, TEST_WEBHOOK_SECRET, "127.0.0.1", *signatures]:
        assert needle not in stored
    assert {r.error for r in results} <= NETWORK_ERRORS | {None}


# --- eligibility, ordering and locking --------------------------------------------


def test_only_due_pending_rows_are_sent_in_deterministic_order(db, queue, receiver, worker_conn):
    events = queue(5)
    ids_ = sorted(event["event_id"] for event in events)
    now = datetime(2030, 1, 1, tzinfo=UTC)
    schedule = {
        ids_[0]: now,  # due exactly now: inclusive boundary
        ids_[1]: now - timedelta(seconds=1),
        ids_[2]: now - timedelta(seconds=1),  # ties break by event_id
        ids_[3]: now + timedelta(microseconds=1),  # not yet due
        ids_[4]: now - timedelta(hours=1),  # already delivered
    }
    for event_id, due in schedule.items():
        db.execute(
            "UPDATE webhook_outbox SET next_attempt_at = %s WHERE event_id = %s", (due, event_id)
        )
    db.execute(
        "UPDATE webhook_outbox SET status = 'delivered', delivered_at = now(), attempts = 1 "
        "WHERE event_id = %s",
        (ids_[4],),
    )
    untouched = [row(db, ids_[3]), row(db, ids_[4])]

    sent = []
    while (result := deliver(worker_conn, receiver.url(), clock=Clock(now))) is not None:
        sent.append(result.event_id)

    assert sent == [ids_[1], ids_[2], ids_[0]]
    assert [UUID(json.loads(r.body)["event_id"]) for r in receiver.requests] == sent
    assert [row(db, ids_[3]), row(db, ids_[4])] == untouched


def test_dead_letter_rows_are_never_polled(db, queue, receiver, worker_conn):
    queue()
    receiver.responder = lambda request: Reply(400)
    assert deliver(worker_conn, receiver.url()).outcome == "dead_letter"
    receiver.responder = lambda request: Reply(200)

    far_future = Clock(datetime(2099, 1, 1, tzinfo=UTC))
    assert deliver(worker_conn, receiver.url(), clock=far_future) is None
    assert len(receiver.requests) == 1


def test_in_flight_lock_is_held_through_persistence_and_skipped_by_others(
    db, pool, ids, queue, receiver, monkeypatch
):
    events = queue(2)
    first_arrived = threading.Event()
    release_first = threading.Event()

    def respond(request):
        if not first_arrived.is_set():
            first_arrived.set()
            release_first.wait(10)
        return Reply(200)

    receiver.responder = respond
    held_at_persist = []
    original = webhooks._mark_delivered

    def checking_mark_delivered(cur, event_id, *args):
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as probe:
            try:
                probe.execute(
                    "SELECT 1 FROM webhook_outbox WHERE event_id = %s FOR UPDATE NOWAIT",
                    (event_id,),
                )
                held_at_persist.append(False)
            except psycopg.errors.LockNotAvailable:
                held_at_persist.append(True)
        return original(cur, event_id, *args)

    monkeypatch.setattr(webhooks, "_mark_delivered", checking_mark_delivered)

    def worker():
        with psycopg.connect(TEST_DATABASE_URL) as conn:
            return deliver(conn, receiver.url())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(worker)
        assert first_arrived.wait(10)
        in_flight = UUID(json.loads(receiver.requests[0].body)["event_id"])
        other = next(e["event_id"] for e in events if e["event_id"] != in_flight)

        with (
            psycopg.connect(TEST_DATABASE_URL, autocommit=True) as probe,
            pytest.raises(psycopg.errors.LockNotAvailable),
        ):
            probe.execute(
                "SELECT 1 FROM webhook_outbox WHERE event_id = %s FOR UPDATE NOWAIT",
                (in_flight,),
            )
        assert row(db, in_flight)["status"] == "pending"

        # A second worker skips the locked row and delivers the other one.
        second = executor.submit(worker).result(timeout=10)
        assert second.event_id == other and second.outcome == "delivered"

        # Money keeps moving between the same accounts while the event is in flight.
        transfer = executor.submit(
            execute_transfer,
            pool,
            user_id=user_id(db, "alice"),
            idempotency_key="during-delivery",
            from_account_id=ids["alice"]["Checking"],
            to_account_id=ids["bob"]["Checking"],
            amount_cents=500,
        ).result(timeout=5)
        assert transfer.replayed is False
        assert not first.done()

        release_first.set()
        result = first.result(timeout=10)

    assert result.event_id == in_flight and result.outcome == "delivered"
    assert held_at_persist == [True, True]
    assert row(db, in_flight)["status"] == "delivered"


# --- transaction faults ----------------------------------------------------------


def test_rolled_back_retry_outcome_is_not_recorded(db, queue, receiver, worker_conn, monkeypatch):
    [event] = queue()
    receiver.responder = lambda request: Reply(503)
    before = row(db, event["event_id"])

    def fail(*args, **kwargs):
        raise RuntimeError("simulated failure while recording")

    monkeypatch.setattr(webhooks, "_schedule_retry", fail)
    with pytest.raises(RuntimeError):
        deliver(worker_conn, receiver.url())
    assert row(db, event["event_id"]) == before
    monkeypatch.undo()

    result = deliver(worker_conn, receiver.url())
    assert (result.outcome, result.attempt) == ("retry", 1)
    assert row(db, event["event_id"])["attempts"] == 1
    assert len(receiver.requests) == 2


def test_failed_dead_letter_move_keeps_event_in_outbox(db, queue, receiver, worker_conn):
    [event] = queue()
    receiver.responder = lambda request: Reply(404)
    # A conflicting dead-letter row makes the move's INSERT fail inside its transaction.
    db.execute(
        "INSERT INTO webhook_dead_letter (event_id, event_type, transfer_id, body, attempts, "
        "last_error, created_at) VALUES (gen_random_uuid(), %s, %s, %s, 1, 'timeout', now())",
        (event["event_type"], event["transfer_id"], event["body"]),
    )
    before_outbox, before_dead = outbox_events(db), dead_letters(db)

    with pytest.raises(psycopg.errors.UniqueViolation):
        deliver(worker_conn, receiver.url())

    assert outbox_events(db) == before_outbox
    assert dead_letters(db) == before_dead
    db.execute("DELETE FROM webhook_dead_letter")

    result = deliver(worker_conn, receiver.url())
    assert (result.outcome, result.attempt) == ("dead_letter", 1)
    assert outbox_events(db) == []
    assert [d["event_id"] for d in dead_letters(db)] == [event["event_id"]]


# --- worker loop -------------------------------------------------------------------


def worker_state(db) -> dict:
    rows = db.execute(
        "SELECT a.state, (SELECT count(*) FROM pg_locks l WHERE l.pid = a.pid) AS locks "
        "FROM pg_stat_activity a WHERE a.application_name = %s AND a.datname = current_database()",
        (webhook_worker.APPLICATION_NAME,),
    ).fetchall()
    return {"states": sorted(r["state"] for r in rows), "locks": sum(r["locks"] for r in rows)}


def test_worker_pool_is_small_and_dedicated():
    pool = webhook_worker.make_worker_pool(TEST_DATABASE_URL)
    try:
        assert (pool.min_size, pool.max_size) == (1, 1)
        assert pool.kwargs["application_name"] == webhook_worker.APPLICATION_NAME
    finally:
        pool.close()


def test_idle_and_backoff_waits_hold_no_connection_or_lock(db, queue, receiver, webhook_config):
    queue()
    receiver.responder = lambda request: Reply(503)
    settings = get_settings()
    stop = threading.Event()
    observations = []
    pool = webhook_worker.make_worker_pool(TEST_DATABASE_URL)
    pool.open(wait=True, timeout=10)

    def wait(seconds):
        stats = pool.get_stats()
        observations.append(
            {
                "checked_out": stats["pool_size"] - stats["pool_available"],
                **worker_state(db),
                "seconds": seconds,
            }
        )
        if len(observations) == 2:
            stop.set()
        return stop.is_set()

    try:
        code = webhook_worker.run(
            pool,
            settings,
            once=False,
            poll_interval=0.25,
            stop=stop,
            uniform=upper,
            wait=wait,
        )
    finally:
        pool.close()

    assert code == 0
    assert len(receiver.requests) == 1  # backoff: the retry is not due yet
    assert observations == [
        {"checked_out": 0, "states": ["idle"], "locks": 0, "seconds": 0.25},
        {"checked_out": 0, "states": ["idle"], "locks": 0, "seconds": 0.25},
    ]
    assert outbox_events(db)[0]["attempts"] == 1


def test_once_attempts_each_due_event_at_most_once(db, queue, receiver, webhook_config):
    queue(3)
    receiver.responder = lambda request: Reply(503)
    pool = webhook_worker.make_worker_pool(TEST_DATABASE_URL)
    pool.open(wait=True, timeout=10)
    try:
        # Zero jitter makes every retry due immediately; --once still stops.
        code = webhook_worker.run(
            pool, get_settings(), once=True, poll_interval=1, stop=threading.Event(), uniform=lower
        )
    finally:
        pool.close()

    assert code == 0
    assert len(receiver.requests) == 3
    assert [event["attempts"] for event in outbox_events(db)] == [1, 1, 1]


def test_api_lifespan_starts_no_delivery(db, queue, receiver, client):
    queue()
    from app import main

    assert "webhook_worker" not in Path(main.__file__).read_text()
    assert client.get("/ui/login").status_code == 200
    time.sleep(0.5)
    assert receiver.requests == []
    assert outbox_events(db)[0]["status"] == "pending"
