from concurrent.futures import ThreadPoolExecutor

from app.ledger import execute_transfer
from tests.helpers import balance, count, ledger_total, post_transfer


def test_repeated_key_returns_original_and_moves_money_once(db, client, alice, ids):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    start_source, start_destination = balance(db, source), balance(db, destination)
    entries_before = count(db, "ledger_entries")

    first = post_transfer(client, alice, "key-1", source, destination, 2_500)
    second = post_transfer(client, alice, "key-1", source, destination, 2_500)
    third = post_transfer(client, alice, "key-1", source, destination, 2_500)

    assert first.status_code == second.status_code == third.status_code == 201
    assert first.json() == second.json() == third.json()
    assert "idempotent-replay" not in first.headers
    assert second.headers["idempotent-replay"] == "true"

    assert balance(db, source) == start_source - 2_500
    assert balance(db, destination) == start_destination + 2_500
    assert count(db, "ledger_entries") == entries_before + 2
    assert ledger_total(db) == 0


def test_replay_returns_original_even_after_balance_changed(db, client, alice, ids):
    source, destination = ids["alice"]["Savings"], ids["bob"]["Savings"]
    full = balance(db, source)

    first = post_transfer(client, alice, "drain", source, destination, full)
    assert first.status_code == 201
    assert balance(db, source) == 0

    # Would be an overdraft if re-evaluated; a replay must not re-run business rules.
    again = post_transfer(client, alice, "drain", source, destination, full)
    assert again.status_code == 201
    assert again.json() == first.json()
    assert balance(db, source) == 0


def test_same_key_with_different_payload_is_rejected(db, client, alice, ids):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    assert post_transfer(client, alice, "key-2", source, destination, 100).status_code == 201
    entries_before = count(db, "ledger_entries")

    conflicting = post_transfer(client, alice, "key-2", source, destination, 200)

    assert conflicting.status_code == 422
    assert count(db, "ledger_entries") == entries_before


def test_keys_are_scoped_per_user(db, client, alice, bob, ids):
    first = post_transfer(
        client, alice, "shared", ids["alice"]["Checking"], ids["bob"]["Checking"], 10
    )
    second = post_transfer(
        client, bob, "shared", ids["bob"]["Checking"], ids["alice"]["Checking"], 20
    )

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] != second.json()["id"]


def test_failed_transfer_does_not_burn_the_key(db, client, alice, ids):
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    too_much = balance(db, source) + 1

    assert post_transfer(client, alice, "retry", source, destination, too_much).status_code == 422
    assert count(db, "transfers") == 4  # only the seed funding transfers

    assert post_transfer(client, alice, "retry", source, destination, 50).status_code == 201


def test_idempotency_key_header_is_required(client, alice, ids):
    response = client.post(
        "/transfers",
        headers=alice,
        json={
            "from_account_id": ids["alice"]["Checking"],
            "to_account_id": ids["bob"]["Checking"],
            "amount_cents": 100,
        },
    )
    assert response.status_code == 422


def test_concurrent_requests_with_same_key_move_money_once(db, pool, ids):
    user_id = db.execute("SELECT id FROM users WHERE email = 'alice@example.com'").fetchone()["id"]
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    start_source, start_destination = balance(db, source), balance(db, destination)

    def attempt(_: int):
        return execute_transfer(
            pool,
            user_id=user_id,
            idempotency_key="race",
            from_account_id=source,
            to_account_id=destination,
            amount_cents=7_000,
        )

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(attempt, range(10)))

    assert len({result.id for result in results}) == 1
    assert sum(not result.replayed for result in results) == 1
    assert balance(db, source) == start_source - 7_000
    assert balance(db, destination) == start_destination + 7_000
    assert ledger_total(db) == 0
