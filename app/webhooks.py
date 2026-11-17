"""Transactional webhook outbox and its delivery.

Events are written on the caller's connection, inside the caller's transaction,
so an event commits or rolls back together with the money movement it reports.
The request path never touches the network: delivery helpers below run only in
the separate worker process (app.webhook_worker).
"""

import contextlib
import hashlib
import hmac
import http.client
import json
import random
import socket
import ssl
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

TRANSFER_COMPLETED = "transfer.completed"
# Recorded delivery outcomes per cycle before an event is dead-lettered. The
# schema's attempts CHECK uses the same bound.
MAX_ATTEMPTS = 6


def utc_timestamp(value: datetime) -> str:
    """RFC 3339 UTC with microseconds, e.g. 2026-01-02T03:04:05.123456Z."""
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def transfer_completed_body(
    *, event_id: UUID, transfer_id: UUID, amount_cents: int, currency: str, created_at: datetime
) -> str:
    """The exact JSON sent to receivers. It is stored once and never rebuilt."""
    return json.dumps(
        {
            "event_id": str(event_id),
            "type": TRANSFER_COMPLETED,
            "transfer_id": str(transfer_id),
            "amount_cents": int(amount_cents),
            "currency": currency,
            "created_at": utc_timestamp(created_at),
        },
        separators=(",", ":"),
    )


def enqueue_transfer_completed(
    conn: psycopg.Connection,
    *,
    transfer_id: UUID,
    amount_cents: int,
    currency: str,
    created_at: datetime,
) -> UUID:
    """Queue a transfer.completed event in the open transaction; returns its id.

    Errors propagate so the caller's transaction (and the transfer) rolls back.
    """
    event_id = uuid4()
    body = transfer_completed_body(
        event_id=event_id,
        transfer_id=transfer_id,
        amount_cents=amount_cents,
        currency=currency,
        created_at=created_at,
    )
    conn.execute(
        "INSERT INTO webhook_outbox (event_id, event_type, transfer_id, body) "
        "VALUES (%s, %s, %s, %s)",
        (event_id, TRANSFER_COMPLETED, transfer_id, body),
    )
    return event_id


# --- Delivery ------------------------------------------------------------------
# Used only by the separate worker process (app.webhook_worker).

SIGNATURE_HEADER = "X-LedgerLite-Signature"
USER_AGENT = "LedgerLite-Webhooks/1"
MAX_RETRY_DELAY_SECONDS = 300
BASE_RETRY_DELAY_SECONDS = 5

# Fixed, URL-free classifications: the only error text ever stored or logged.
# All of them are transport failures and are retried.
NETWORK_ERRORS = frozenset(
    {
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
)
# A status line outside 100-599: terminal, like any other unexpected response.
INVALID_STATUS = "invalid_status"

CLAIM_DUE_EVENT = (
    "SELECT event_id, event_type, transfer_id, body, attempts, created_at "
    "FROM webhook_outbox "
    "WHERE status = 'pending' AND next_attempt_at <= %s AND NOT (event_id = ANY(%s::uuid[])) "
    "ORDER BY next_attempt_at, event_id "
    "LIMIT 1 FOR UPDATE SKIP LOCKED"
)


@dataclass(frozen=True)
class Timeouts:
    """Seconds. ``total`` caps the whole request, including slow trickled responses."""

    connect: float = 5.0
    read: float = 10.0
    total: float = 15.0


DEFAULT_TIMEOUTS = Timeouts()


@dataclass(frozen=True)
class SendResult:
    status_code: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    event_id: UUID
    attempt: int
    outcome: str  # "delivered", "retry" or "dead_letter"
    status_code: int | None
    error: str | None
    next_attempt_at: datetime | None = None


def utc_now() -> datetime:
    return datetime.now(UTC)


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """X-LedgerLite-Signature value: HMAC-SHA256 over ASCII timestamp + "." + body."""
    signed = str(int(timestamp)).encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={int(timestamp)},v1={digest}"


def retry_delay(attempt: int, uniform: Callable[[float, float], float] = random.uniform) -> float:
    """Full-jitter backoff after recorded failure ``attempt`` (1-based), in seconds."""
    cap = min(MAX_RETRY_DELAY_SECONDS, BASE_RETRY_DELAY_SECONDS * 2 ** (attempt - 1))
    return uniform(0, cap)


def classify_error(exc: BaseException) -> str:
    """Map a transport exception to a fixed label. Messages are never kept: they
    can carry hostnames, URLs or response fragments."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_certificate_invalid"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, socket.gaierror):
        return "dns_error"
    if isinstance(exc, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, http.client.RemoteDisconnected):
        return "connection_closed"
    if isinstance(exc, ConnectionError):
        return "connection_error"
    if isinstance(exc, http.client.HTTPException):
        return "invalid_response"
    return "network_error"


@cache
def _tls_context() -> ssl.SSLContext:
    # Certificate and hostname verification are on (CERT_REQUIRED, check_hostname).
    return ssl.create_default_context()


def post_webhook(
    url: str, body: bytes, headers: dict[str, str], timeouts: Timeouts = DEFAULT_TIMEOUTS
) -> SendResult:
    """Send exactly one POST; never follows redirects or retries.

    Only the status line and headers are read; the response body is discarded
    unread, so a large or endless response cannot hold the worker.
    """
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    # An explicit port: http.client would read a bare IPv6 host such as "::1" as host:port.
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(
            parts.hostname,
            parts.port or http.client.HTTPS_PORT,
            timeout=timeouts.connect,
            context=_tls_context(),
        )
    else:
        conn = http.client.HTTPConnection(
            parts.hostname, parts.port or http.client.HTTP_PORT, timeout=timeouts.connect
        )

    expired = threading.Event()

    def expire() -> None:
        # Unblocks a recv that keeps receiving a byte just inside the read timeout.
        expired.set()
        sock = conn.sock
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    deadline = threading.Timer(timeouts.total, expire)
    deadline.daemon = True
    deadline.start()
    try:
        conn.connect()
        # Before the socket existed, expire() had nothing to shut down (DNS has no
        # timeout of its own), so a deadline passed during connect stops here.
        if expired.is_set():
            return SendResult(error="timeout")
        conn.sock.settimeout(timeouts.read)
        conn.request("POST", target, body=body, headers=headers)
        response = conn.getresponse()
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        return SendResult(error="timeout" if expired.is_set() else classify_error(exc))
    finally:
        deadline.cancel()
        conn.close()
    if expired.is_set():
        return SendResult(error="timeout")
    if not 100 <= status <= 599:
        return SendResult(error=INVALID_STATUS)
    return SendResult(status_code=status)


def _outcome(result: SendResult, attempt: int) -> str:
    if result.status_code is not None and 200 <= result.status_code <= 299:
        return "delivered"
    retryable = result.error in NETWORK_ERRORS or (
        result.status_code is not None
        and (result.status_code == 429 or 500 <= result.status_code <= 599)
    )
    # Other 4xx (408 included), redirects and odd responses are terminal.
    if retryable and attempt < MAX_ATTEMPTS:
        return "retry"
    return "dead_letter"


def _mark_delivered(
    cur: psycopg.Cursor, event_id: UUID, attempt: int, result: SendResult, at: datetime
) -> None:
    cur.execute(
        "UPDATE webhook_outbox SET status = 'delivered', attempts = %s, "
        "last_status_code = %s, last_error = NULL, delivered_at = %s, updated_at = %s "
        "WHERE event_id = %s",
        (attempt, result.status_code, at, at, event_id),
    )


def _schedule_retry(
    cur: psycopg.Cursor,
    event_id: UUID,
    attempt: int,
    result: SendResult,
    at: datetime,
    next_attempt_at: datetime,
) -> None:
    cur.execute(
        "UPDATE webhook_outbox SET attempts = %s, next_attempt_at = %s, "
        "last_status_code = %s, last_error = %s, updated_at = %s WHERE event_id = %s",
        (attempt, next_attempt_at, result.status_code, result.error, at, event_id),
    )


def _move_to_dead_letter(
    cur: psycopg.Cursor, event_id: UUID, attempt: int, result: SendResult, at: datetime
) -> None:
    # One statement: the event leaves the outbox only if it lands in the dead letter.
    cur.execute(
        "WITH moved AS (DELETE FROM webhook_outbox WHERE event_id = %s "
        "RETURNING event_id, event_type, transfer_id, body, created_at) "
        "INSERT INTO webhook_dead_letter (event_id, event_type, transfer_id, body, attempts, "
        "last_status_code, last_error, created_at, failed_at) "
        "SELECT event_id, event_type, transfer_id, body, %s, %s, %s, created_at, %s FROM moved",
        (event_id, attempt, result.status_code, result.error, at),
    )
    if cur.rowcount != 1:
        raise RuntimeError("dead-letter move did not find the claimed event")


def deliver_next(
    conn: psycopg.Connection,
    *,
    url: str,
    secret: str,
    clock: Callable[[], datetime] = utc_now,
    uniform: Callable[[float, float], float] = random.uniform,
    due_at: datetime | None = None,
    exclude: Sequence[UUID] = (),
    timeouts: Timeouts = DEFAULT_TIMEOUTS,
) -> DeliveryResult | None:
    """Make one delivery attempt for the next due event, or return None.

    The event's row lock (and only that lock) is held from the claim through the
    HTTP request and the outcome update, all in one transaction. Locked rows are
    skipped, so concurrent workers send different events. If the outcome cannot
    be written, the transaction rolls back and the attempt is not counted.
    """
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        event = cur.execute(
            CLAIM_DUE_EVENT, (due_at if due_at is not None else clock(), list(exclude))
        ).fetchone()
        if event is None:
            return None

        event_id = event["event_id"]
        body = event["body"].encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Idempotency-Key": str(event_id),
            SIGNATURE_HEADER: signature(secret, int(clock().timestamp()), body),
        }
        result = post_webhook(url, body, headers, timeouts)

        finished = clock()
        attempt = min(event["attempts"] + 1, MAX_ATTEMPTS)
        outcome = _outcome(result, attempt)
        next_attempt_at = None
        if outcome == "delivered":
            _mark_delivered(cur, event_id, attempt, result, finished)
        elif outcome == "retry":
            next_attempt_at = finished + timedelta(seconds=retry_delay(attempt, uniform))
            _schedule_retry(cur, event_id, attempt, result, finished, next_attempt_at)
        else:
            _move_to_dead_letter(cur, event_id, attempt, result, finished)

    return DeliveryResult(
        event_id=event_id,
        attempt=attempt,
        outcome=outcome,
        status_code=result.status_code,
        error=result.error,
        next_attempt_at=next_attempt_at,
    )


# --- Dead-letter administration --------------------------------------------------
# Used by the admin API. Only queue rows change; transfers and the ledger never do.

# Hard cap on one listing response, whatever limit the caller asks for.
DEAD_LETTER_LIST_MAX = 100

DEAD_LETTER_COLUMNS = (
    "event_id, event_type, transfer_id, attempts, last_status_code, last_error, "
    "created_at, failed_at"
)


class ReplayNotFound(Exception):
    """No dead-letter event has that id (unknown, still queued, or already replayed)."""


class ReplayConflict(Exception):
    """The outbox already holds an event with that identity, so nothing was moved."""


def list_dead_letters(pool: ConnectionPool, limit: int = DEAD_LETTER_LIST_MAX) -> list[dict]:
    """Oldest failures first. Bodies are not returned, only delivery metadata."""
    limit = max(1, min(int(limit), DEAD_LETTER_LIST_MAX))
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            f"SELECT {DEAD_LETTER_COLUMNS} FROM webhook_dead_letter "  # noqa: S608
            "ORDER BY failed_at, event_id LIMIT %s",
            (limit,),
        ).fetchall()


def _requeue(cur: psycopg.Cursor, event: dict, at: datetime) -> dict:
    return cur.execute(
        "INSERT INTO webhook_outbox (event_id, event_type, transfer_id, body, status, "
        "attempts, next_attempt_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, 'pending', 0, %s, %s, %s) "
        "RETURNING event_id, transfer_id, status, attempts, next_attempt_at",
        (
            event["event_id"],
            event["event_type"],
            event["transfer_id"],
            event["body"],
            at,
            event["created_at"],
            at,
        ),
    ).fetchone()


def replay_dead_letter(
    pool: ConnectionPool, event_id: UUID, *, clock: Callable[[], datetime] = utc_now
) -> dict:
    """Move one dead-letter event back to the outbox, due now, with a fresh
    attempt budget and its original id, body and timestamps.

    The DELETE takes the dead row's lock, so of two concurrent replays only the
    first finds the row; the other waits, then finds nothing and raises
    ReplayNotFound. Any failure rolls the move back, leaving the dead row as it was.
    """
    try:
        with (
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            event = cur.execute(
                "DELETE FROM webhook_dead_letter WHERE event_id = %s "
                "RETURNING event_id, event_type, transfer_id, body, created_at",
                (event_id,),
            ).fetchone()
            if event is None:
                raise ReplayNotFound
            return _requeue(cur, event, clock())
    except psycopg.errors.UniqueViolation:
        raise ReplayConflict from None
