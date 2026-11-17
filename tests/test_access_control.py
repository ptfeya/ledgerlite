import time

import jwt
import pytest

from app.config import get_settings
from tests.helpers import api_login, balance, count, page_login, post_transfer


def test_login_returns_a_token(client):
    response = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "test-password"}
    )
    assert response.status_code == 200
    assert response.json()["token_type"] == "bearer"
    assert response.json()["access_token"]


@pytest.mark.parametrize(
    ("email", "password"),
    [("alice@example.com", "wrong"), ("nobody@example.com", "test-password")],
)
def test_bad_credentials_get_401(client, email, password):
    response = client.post("/auth/login", json={"email": email, "password": password})
    assert response.status_code == 401


def test_accounts_returns_only_the_callers_accounts(client, alice, bob, ids):
    mine = client.get("/accounts", headers=alice).json()
    theirs = client.get("/accounts", headers=bob).json()

    assert {a["id"] for a in mine} == set(ids["alice"].values())
    assert {a["id"] for a in theirs} == set(ids["bob"].values())
    assert {a["name"] for a in mine} == {"Checking", "Savings"}
    assert all(isinstance(a["balance_cents"], int) for a in mine)


def test_user_cannot_read_another_users_entries(client, alice, bob, ids):
    foreign = client.get(f"/accounts/{ids['bob']['Checking']}/entries", headers=alice)
    missing = client.get("/accounts/999999/entries", headers=alice)

    assert foreign.status_code == 404
    # Indistinguishable from a nonexistent account, so ids cannot be probed.
    assert foreign.json() == missing.json()


def test_user_can_read_own_entries(client, alice, ids):
    response = client.get(f"/accounts/{ids['alice']['Checking']}/entries", headers=alice)

    assert response.status_code == 200
    entries = response.json()
    assert [e["amount_cents"] for e in entries] == [100_000]  # opening balance

    post_transfer(client, alice, "k", ids["alice"]["Checking"], ids["bob"]["Checking"], 500)
    entries = client.get(f"/accounts/{ids['alice']['Checking']}/entries", headers=alice).json()
    assert [e["amount_cents"] for e in entries] == [-500, 100_000]  # newest first


def test_user_cannot_transfer_out_of_another_users_account(db, client, alice, ids):
    victim = ids["bob"]["Checking"]
    start = balance(db, victim)
    entries_before = count(db, "ledger_entries")

    response = post_transfer(client, alice, "steal", victim, ids["alice"]["Checking"], 100)

    assert response.status_code == 404
    assert balance(db, victim) == start
    assert count(db, "ledger_entries") == entries_before


def test_user_can_send_money_to_another_users_account(db, client, alice, ids):
    start = balance(db, ids["bob"]["Checking"])
    response = post_transfer(
        client, alice, "pay", ids["alice"]["Checking"], ids["bob"]["Checking"], 300
    )
    assert response.status_code == 201
    assert balance(db, ids["bob"]["Checking"]) == start + 300


def test_non_admin_cannot_reconcile(client, alice, monkeypatch):
    def forbidden_ledger_read(pool):
        pytest.fail("Non-admins must not run the ledger-wide check")

    monkeypatch.setattr("app.routes.api.ledger.ledger_imbalance", forbidden_ledger_read)
    response = client.post("/reconcile", headers=alice)

    assert response.status_code == 403


def test_admin_access_is_checked_on_each_request(db, client, admin):
    assert client.post("/reconcile", headers=admin).status_code == 200

    db.execute("UPDATE users SET is_admin = false WHERE email = 'admin@example.com'")
    assert client.post("/reconcile", headers=admin).status_code == 403

    db.execute("UPDATE users SET is_admin = true WHERE email = 'admin@example.com'")
    assert client.post("/reconcile", headers=admin).status_code == 200


def test_deleted_admin_token_is_rejected(db, client, admin):
    db.execute("DELETE FROM users WHERE email = 'admin@example.com'")

    assert client.post("/reconcile", headers=admin).status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/accounts"),
        ("POST", "/transfers"),
        ("GET", "/accounts/1/entries"),
        ("POST", "/reconcile"),
    ],
)
def test_api_requires_a_token(client, method, path):
    assert client.request(method, path).status_code == 401


@pytest.mark.parametrize(("method", "path"), [("GET", "/accounts"), ("POST", "/reconcile")])
def test_invalid_and_expired_tokens_are_rejected(client, method, path):
    secret = get_settings().jwt_secret
    expired = jwt.encode({"sub": "1", "exp": int(time.time()) - 10}, secret, algorithm="HS256")
    forged = jwt.encode({"sub": "1", "exp": int(time.time()) + 600}, "x" * 32, algorithm="HS256")

    for token in (expired, forged, "garbage"):
        response = client.request(method, path, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401


@pytest.mark.parametrize("user", ["alice", "admin"])
def test_session_cookie_does_not_authenticate_the_json_api(client, user):
    assert page_login(client, user).status_code == 303
    assert client.cookies.get("ll_session")

    # The cookie is for HTML pages only. State-changing API routes ignore it, so a
    # cross-site request carrying the cookie cannot act as the user.
    assert client.post("/reconcile").status_code == 401
    assert client.post("/transfers", headers={"Idempotency-Key": "k"}, json={}).status_code == 401
    assert api_login(client, "alice")  # bearer login still works


def test_admin_cookie_does_not_elevate_a_non_admin_token(client, alice):
    assert page_login(client, "admin").status_code == 303

    assert client.post("/reconcile", headers=alice).status_code == 403
    assert client.post("/reconcile", headers={"Authorization": "Bearer invalid"}).status_code == 401
