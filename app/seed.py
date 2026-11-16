"""Apply the schema and seed demo data: `uv run python -m app.seed`."""

from functools import lru_cache

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.db import apply_schema
from app.security import hash_password

SYSTEM_ACCOUNT_NAME = "System funding"

# email -> [(account name, opening balance in cents)]
SEED_USERS: dict[str, list[tuple[str, int]]] = {
    "alice@example.com": [("Checking", 100_000), ("Savings", 50_000)],
    "bob@example.com": [("Checking", 75_000), ("Savings", 25_000)],
}


@lru_cache
def _hash(password: str) -> str:
    return hash_password(password)


def seed(conn: psycopg.Connection, password: str) -> bool:
    """Create users, accounts and opening balances. Returns False if already seeded.

    Opening balances are funded from a system account (no owning user) with
    balanced entries, so the ledger sums to zero from the first row.
    """
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return False

    system_id = conn.execute(
        "INSERT INTO accounts (name, is_system) VALUES (%s, true) RETURNING id",
        (SYSTEM_ACCOUNT_NAME,),
    ).fetchone()["id"]

    for email, accounts in SEED_USERS.items():
        user_id = conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING id",
            (email, _hash(password)),
        ).fetchone()["id"]
        for name, opening_cents in accounts:
            account_id = conn.execute(
                "INSERT INTO accounts (user_id, name) VALUES (%s, %s) RETURNING id",
                (user_id, name),
            ).fetchone()["id"]
            transfer_id = conn.execute(
                "INSERT INTO transfers (user_id, idempotency_key, request_hash, "
                "from_account_id, to_account_id, amount_cents) "
                "VALUES (NULL, %s, 'seed', %s, %s, %s) RETURNING id",
                (f"seed-funding-{account_id}", system_id, account_id, opening_cents),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
                "VALUES (%s, %s, %s), (%s, %s, %s)",
                (transfer_id, system_id, -opening_cents, transfer_id, account_id, opening_cents),
            )
    conn.commit()
    return True


def main() -> None:
    settings = get_settings()
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        apply_schema(conn)
        created = seed(conn, settings.seed_password)
    print("Seeded demo data." if created else "Already seeded; nothing to do.")


if __name__ == "__main__":
    main()
