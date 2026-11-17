import psycopg
import pytest

from tests.helpers import balance, count, csrf_from_html, ledger_total, page_login, post_transfer


def test_system_account_has_no_owner_and_no_login(db, client):
    system = db.execute("SELECT id, user_id FROM accounts WHERE is_system").fetchone()
    assert system["user_id"] is None
    assert db.execute("SELECT COUNT(*) AS n FROM accounts WHERE is_system").fetchone()["n"] == 1

    for email, expected_status in (
        ("system@example.com", 401),
        ("system", 401),
        ("system-funding@example.com", 401),
        ("", 422),
    ):
        response = client.post("/auth/login", json={"email": email, "password": "test-password"})
        assert response.status_code == expected_status
        assert "access_token" not in response.json()


def test_database_forbids_giving_the_system_account_an_owner(db):
    user_id = db.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE accounts SET user_id = %s WHERE is_system", (user_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO accounts (name, is_system, user_id) VALUES ('x', true, %s)", (user_id,)
        )


def test_system_account_cannot_be_a_transfer_source(db, client, alice, bob, ids):
    system = ids["system"]
    start = balance(db, system)
    assert start < 0  # it funded the seed balances
    entries_before = count(db, "ledger_entries")

    for headers, key, destination in (
        (alice, "s-a", ids["alice"]["Checking"]),
        (bob, "s-b", ids["bob"]["Checking"]),
    ):
        response = post_transfer(client, headers, key, system, destination, 100)
        assert response.status_code == 404

    assert balance(db, system) == start
    assert count(db, "ledger_entries") == entries_before
    assert ledger_total(db) == 0


def test_system_account_cannot_be_a_transfer_destination(db, client, alice, ids):
    entries_before = count(db, "ledger_entries")
    response = post_transfer(client, alice, "to-sys", ids["alice"]["Checking"], ids["system"], 100)
    assert response.status_code == 404
    assert count(db, "ledger_entries") == entries_before


def test_system_account_is_invisible_through_the_api(client, alice, bob, ids):
    for headers in (alice, bob):
        listed = {a["id"] for a in client.get("/accounts", headers=headers).json()}
        assert ids["system"] not in listed
        assert client.get(f"/accounts/{ids['system']}/entries", headers=headers).status_code == 404


def test_system_account_cannot_be_a_source_via_the_html_form(db, client, ids):
    assert page_login(client, "alice").status_code == 303
    token = csrf_from_html(client.get("/ui/transfer").text)
    entries_before = count(db, "ledger_entries")

    response = client.post(
        "/ui/transfer",
        data={
            "from_account_id": str(ids["system"]),
            "to_email": "bob@example.com",
            "amount": "10.00",
            "idempotency_key": "form-sys",
            "csrf_token": token,
        },
    )

    assert response.status_code == 404
    assert count(db, "ledger_entries") == entries_before
