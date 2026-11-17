"""Transactional webhook outbox.

Events are written on the caller's connection, inside the caller's transaction,
so an event commits or rolls back together with the money movement it reports.
Nothing here touches the network: delivery is a separate worker process.
"""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg

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
