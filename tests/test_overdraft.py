from concurrent.futures import ThreadPoolExecutor

import pytest

from app.ledger import InsufficientFunds, execute_transfer
from tests.helpers import balance, count, ledger_total, post_transfer


def test_overdraft_is_rejected_and_changes_nothing(db, client, alice, ids):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    start_source, start_destination = balance(db, source), balance(db, destination)
    transfers_before, entries_before = count(db, "transfers"), count(db, "ledger_entries")

    response = post_transfer(client, alice, "od-1", source, destination, start_source + 1)

    assert response.status_code == 422
    assert "insufficient" in response.json()["detail"].lower()
    assert balance(db, source) == start_source
    assert balance(db, destination) == start_destination
    assert count(db, "transfers") == transfers_before
    assert count(db, "ledger_entries") == entries_before


def test_spending_exactly_the_balance_is_allowed(db, client, alice, ids):
    source, destination = ids["alice"]["Savings"], ids["alice"]["Checking"]
    full = balance(db, source)

    assert post_transfer(client, alice, "exact", source, destination, full).status_code == 201
    assert balance(db, source) == 0
    assert post_transfer(client, alice, "one-more", source, destination, 1).status_code == 422
    assert ledger_total(db) == 0


def test_concurrent_transfers_cannot_overdraw(db, pool, ids):
    user_id = db.execute("SELECT id FROM users WHERE email = 'alice@example.com'").fetchone()["id"]
    source, destination = ids["alice"]["Savings"], ids["bob"]["Savings"]
    funds = balance(db, source)
    amount = funds // 2 + 1  # any two of these exceed the balance

    def attempt(i: int) -> bool:
        try:
            execute_transfer(
                pool,
                user_id=user_id,
                idempotency_key=f"race-{i}",
                from_account_id=source,
                to_account_id=destination,
                amount_cents=amount,
            )
            return True
        except InsufficientFunds:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(attempt, range(8)))

    assert sum(outcomes) == 1
    assert balance(db, source) == funds - amount
    assert balance(db, source) >= 0
    assert ledger_total(db) == 0


@pytest.mark.parametrize("amount", [0, -5])
def test_non_positive_amounts_are_rejected(client, alice, ids, amount):
    response = post_transfer(
        client, alice, f"bad-{amount}", ids["alice"]["Checking"], ids["bob"]["Checking"], amount
    )
    assert response.status_code == 422


def test_transfer_to_same_account_is_rejected(client, alice, ids):
    account = ids["alice"]["Checking"]
    assert post_transfer(client, alice, "same", account, account, 100).status_code == 422


def test_unknown_destination_is_rejected(db, client, alice, ids):
    entries_before = count(db, "ledger_entries")
    response = post_transfer(client, alice, "ghost", ids["alice"]["Checking"], 999_999, 100)
    assert response.status_code == 404
    assert count(db, "ledger_entries") == entries_before
