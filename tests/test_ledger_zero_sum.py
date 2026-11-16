from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest

from app.ledger import execute_transfer
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import balance, ledger_total, post_transfer


def test_seeded_ledger_sums_to_zero(db, client, alice):
    assert ledger_total(db) == 0
    assert client.post("/reconcile", headers=alice).json() == {"status": "ok"}


def test_ledger_sums_to_zero_after_many_transfers(db, client, alice, bob, ids):
    a_check, a_save = ids["alice"]["Checking"], ids["alice"]["Savings"]
    b_check = ids["bob"]["Checking"]

    assert post_transfer(client, alice, "t1", a_check, b_check, 12_345).status_code == 201
    assert post_transfer(client, alice, "t2", a_check, a_save, 1).status_code == 201
    assert post_transfer(client, bob, "t3", b_check, a_check, 999).status_code == 201
    assert (
        post_transfer(client, bob, "t4", ids["bob"]["Savings"], a_save, 25_000).status_code == 201
    )

    assert ledger_total(db) == 0
    assert client.post("/reconcile", headers=alice).json() == {"status": "ok"}

    # Every transfer has exactly one debit and one credit of equal size.
    rows = db.execute(
        "SELECT transfer_id, COUNT(*) AS n, SUM(amount_cents) AS total, "
        "MIN(amount_cents) AS low, MAX(amount_cents) AS high "
        "FROM ledger_entries GROUP BY transfer_id"
    ).fetchall()
    assert len(rows) == 4 + 4  # four seed funding transfers + the four above
    for row in rows:
        assert row["n"] == 2
        assert row["total"] == 0
        assert row["low"] == -row["high"]


def test_concurrent_transfers_keep_ledger_balanced(db, pool, ids):
    alice_id = db.execute("SELECT id FROM users WHERE email = 'alice@example.com'").fetchone()["id"]
    bob_id = db.execute("SELECT id FROM users WHERE email = 'bob@example.com'").fetchone()["id"]
    a_check, b_check = ids["alice"]["Checking"], ids["bob"]["Checking"]
    start_a, start_b = balance(db, a_check), balance(db, b_check)

    def move(i: int) -> None:
        # Opposite directions at once: exercises lock ordering (no deadlock).
        if i % 2 == 0:
            execute_transfer(
                pool,
                user_id=alice_id,
                idempotency_key=f"a-{i}",
                from_account_id=a_check,
                to_account_id=b_check,
                amount_cents=100,
            )
        else:
            execute_transfer(
                pool,
                user_id=bob_id,
                idempotency_key=f"b-{i}",
                from_account_id=b_check,
                to_account_id=a_check,
                amount_cents=40,
            )

    with ThreadPoolExecutor(max_workers=10) as executor:
        list(executor.map(move, range(40)))

    assert ledger_total(db) == 0
    assert balance(db, a_check) == start_a - 20 * 100 + 20 * 40
    assert balance(db, b_check) == start_b + 20 * 100 - 20 * 40


def test_reconcile_reports_imbalance(db, client, alice, ids):
    # Bypass the commit-time balance trigger to simulate corruption.
    db.execute("ALTER TABLE ledger_entries DISABLE TRIGGER ledger_entries_balanced")
    try:
        transfer_id = db.execute(
            "INSERT INTO transfers (user_id, idempotency_key, request_hash, from_account_id, "
            "to_account_id, amount_cents) VALUES (NULL, 'corrupt', 'x', %s, %s, 123) RETURNING id",
            (ids["alice"]["Checking"], ids["bob"]["Checking"]),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
            "VALUES (%s, %s, 123)",
            (transfer_id, ids["bob"]["Checking"]),
        )
        response = client.post("/reconcile", headers=alice)
    finally:
        db.execute("ALTER TABLE ledger_entries ENABLE TRIGGER ledger_entries_balanced")

    assert response.status_code == 200
    assert response.json() == {"status": "imbalance", "imbalance_cents": 123}


def test_database_refuses_unbalanced_entries(ids):
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        transfer_id = conn.execute(
            "INSERT INTO transfers (user_id, idempotency_key, request_hash, from_account_id, "
            "to_account_id, amount_cents) VALUES (NULL, 'bad', 'x', %s, %s, 50) RETURNING id",
            (ids["alice"]["Checking"], ids["bob"]["Checking"]),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
            "VALUES (%s, %s, 50)",
            (transfer_id, ids["bob"]["Checking"]),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.commit()


def test_ledger_entries_are_append_only(db):
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE ledger_entries SET amount_cents = amount_cents + 1")
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("DELETE FROM ledger_entries")
