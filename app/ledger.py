import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from app import webhooks
from app.config import get_settings

MAX_AMOUNT_CENTS = 10**12


class TransferError(Exception):
    status_code = 422


class InvalidTransfer(TransferError):
    pass


class AccountNotFound(TransferError):
    status_code = 404


class InsufficientFunds(TransferError):
    pass


class IdempotencyConflict(TransferError):
    pass


@dataclass(frozen=True)
class TransferResult:
    id: UUID
    from_account_id: int
    to_account_id: int
    amount_cents: int
    created_at: datetime
    replayed: bool


def _request_hash(from_account_id: int, to_account_id: int, amount_cents: int) -> str:
    payload = f"{from_account_id}|{to_account_id}|{amount_cents}"
    return hashlib.sha256(payload.encode()).hexdigest()


def resolve_recipient_account(pool: ConnectionPool, *, user_id: int, email: str) -> int:
    """Resolve an email to exactly one non-system Checking account.

    Missing users, missing Checking accounts and ambiguous matches deliberately
    produce the same error. Money movement still goes through execute_transfer.
    """
    email = email.strip().lower()
    if not re.fullmatch(
        r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
        r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*",
        email,
    ):
        raise InvalidTransfer("recipient not found")

    with pool.connection() as conn:
        accounts = conn.execute(
            "SELECT a.id, a.user_id FROM accounts a JOIN users u ON u.id = a.user_id "
            "WHERE u.email = %s AND a.name = 'Checking' AND NOT a.is_system LIMIT 2",
            (email,),
        ).fetchall()

    if len(accounts) != 1:
        raise InvalidTransfer("recipient not found")
    if accounts[0]["user_id"] == user_id:
        raise InvalidTransfer("cannot send to yourself")
    return accounts[0]["id"]


def execute_transfer(
    pool: ConnectionPool,
    *,
    user_id: int,
    idempotency_key: str,
    from_account_id: int,
    to_account_id: int,
    amount_cents: int,
) -> TransferResult:
    """Move money in a single transaction: two ledger entries, or nothing.

    Both accounts are locked in id order (no deadlocks between opposite
    transfers), and the balance check runs under the source lock, so concurrent
    transfers cannot overdraw. A repeated (user, key) returns the stored
    transfer before any balance check, so replays never re-run business rules.

    With webhooks enabled, a new transfer also queues one transfer.completed
    event in the same transaction. Replays never queue (or reset) an event.
    """
    if not 0 < amount_cents <= MAX_AMOUNT_CENTS:
        raise InvalidTransfer("amount must be a positive number of cents")
    if from_account_id == to_account_id:
        raise InvalidTransfer("source and destination must differ")

    request_hash = _request_hash(from_account_id, to_account_id, amount_cents)
    enqueue_webhook = get_settings().webhooks_enabled

    with pool.connection() as conn:
        locked = conn.execute(
            "SELECT id, user_id, currency, is_system FROM accounts "
            "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
            ([from_account_id, to_account_id],),
        ).fetchall()
        accounts = {row["id"]: row for row in locked}
        source = accounts.get(from_account_id)
        destination = accounts.get(to_account_id)

        # The system funding account can never be moved by an API call, and a
        # source that is missing or owned by someone else looks the same.
        if source is None or source["is_system"] or source["user_id"] != user_id:
            raise AccountNotFound("source account not found")
        if destination is None or destination["is_system"]:
            raise AccountNotFound("destination account not found")
        if source["currency"] != destination["currency"]:
            raise InvalidTransfer("currency mismatch")

        created = conn.execute(
            "INSERT INTO transfers (user_id, idempotency_key, request_hash, "
            "from_account_id, to_account_id, amount_cents) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, idempotency_key) DO NOTHING "
            "RETURNING id, created_at",
            (user_id, idempotency_key, request_hash, from_account_id, to_account_id, amount_cents),
        ).fetchone()

        if created is None:
            existing = conn.execute(
                "SELECT id, request_hash, from_account_id, to_account_id, amount_cents, created_at "
                "FROM transfers WHERE user_id = %s AND idempotency_key = %s",
                (user_id, idempotency_key),
            ).fetchone()
            if existing["request_hash"] != request_hash:
                raise IdempotencyConflict(
                    "Idempotency-Key was already used with a different request"
                )
            return TransferResult(
                id=existing["id"],
                from_account_id=existing["from_account_id"],
                to_account_id=existing["to_account_id"],
                amount_cents=existing["amount_cents"],
                created_at=existing["created_at"],
                replayed=True,
            )

        balance = conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS balance "
            "FROM ledger_entries WHERE account_id = %s",
            (from_account_id,),
        ).fetchone()["balance"]
        if balance < amount_cents:
            raise InsufficientFunds("insufficient funds")

        conn.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
            "VALUES (%s, %s, %s), (%s, %s, %s)",
            (
                created["id"],
                from_account_id,
                -amount_cents,
                created["id"],
                to_account_id,
                amount_cents,
            ),
        )
        if enqueue_webhook:
            webhooks.enqueue_transfer_completed(
                conn,
                transfer_id=created["id"],
                amount_cents=amount_cents,
                currency=source["currency"],
                created_at=created["created_at"],
            )

    return TransferResult(
        id=created["id"],
        from_account_id=from_account_id,
        to_account_id=to_account_id,
        amount_cents=amount_cents,
        created_at=created["created_at"],
        replayed=False,
    )


def list_accounts(pool: ConnectionPool, user_id: int) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT a.id, a.name, a.currency, "
            "COALESCE(SUM(e.amount_cents), 0)::bigint AS balance_cents "
            "FROM accounts a LEFT JOIN ledger_entries e ON e.account_id = a.id "
            "WHERE a.user_id = %s GROUP BY a.id ORDER BY a.id",
            (user_id,),
        ).fetchall()


def list_entries(
    pool: ConnectionPool, user_id: int, account_id: int, limit: int = 100
) -> list[dict] | None:
    """Entries for an account the user owns, newest first. None if the account
    does not exist or belongs to someone else (callers answer 404 for both)."""
    with pool.connection() as conn:
        owned = conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s AND user_id = %s", (account_id, user_id)
        ).fetchone()
        if owned is None:
            return None
        return conn.execute(
            "SELECT id, transfer_id, account_id, amount_cents, created_at "
            "FROM ledger_entries WHERE account_id = %s ORDER BY id DESC LIMIT %s",
            (account_id, limit),
        ).fetchall()


def ledger_imbalance(pool: ConnectionPool) -> int:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS total FROM ledger_entries"
        ).fetchone()["total"]


def account_balance(conn: psycopg.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS b FROM ledger_entries "
        "WHERE account_id = %s",
        (account_id,),
    ).fetchone()["b"]
