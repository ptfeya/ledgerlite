import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.helpers import balance, count, csrf_from_html, ledger_total, page_login, transfer_form


def logged_in_token(client, user="alice"):
    assert page_login(client, user).status_code == 303
    return csrf_from_html(client.get("/ui/transfer").text)


# --- pages -----------------------------------------------------------------


def test_pages_require_login(client):
    for path in ("/ui/accounts", "/ui/transfer", "/ui/history"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/ui/login"


def test_login_accounts_transfer_history_flow(db, client, ids):
    token = logged_in_token(client)

    accounts = client.get("/ui/accounts")
    assert "$1,000.00" in accounts.text and "Savings" in accounts.text
    assert "bob@example.com" not in accounts.text

    sent = client.post(
        "/ui/transfer", data=transfer_form(ids, token), headers={"HX-Request": "true"}
    )
    assert sent.status_code == 200
    assert "Sent $12.34" in sent.text
    assert "<html" not in sent.text  # htmx gets a fragment
    assert 'hx-swap-oob="true"' in sent.text
    assert balance(db, ids["bob"]["Checking"]) == 75_000 + 1_234
    assert balance(db, ids["bob"]["Savings"]) == 25_000
    assert ledger_total(db) == 0

    history = client.get(f"/ui/history?account_id={ids['alice']['Checking']}")
    assert "-$12.34" in history.text

    assert client.get(f"/ui/history?account_id={ids['bob']['Checking']}").status_code == 404


def test_transfer_form_asks_for_recipient_email(client):
    logged_in_token(client)
    html = client.get("/ui/transfer").text
    assert 'for="to_email">To (recipient email)</label>' in html
    assert '<input id="to_email" name="to_email" type="email"' in html
    assert "to_account_id" not in html


@pytest.mark.parametrize("email", ["bob@example.com", " \tBOB@EXAMPLE.COM \r\n"])
def test_page_transfer_by_email_uses_checking(db, client, ids, email):
    token = logged_in_token(client)
    entries_before = count(db, "ledger_entries")
    response = client.post("/ui/transfer", data=transfer_form(ids, token, to=email))

    assert response.status_code == 200
    assert "<html" in response.text
    assert "Sent $12.34" in response.text
    assert balance(db, ids["alice"]["Checking"]) == 100_000 - 1_234
    assert balance(db, ids["bob"]["Checking"]) == 75_000 + 1_234
    assert balance(db, ids["bob"]["Savings"]) == 25_000
    assert count(db, "ledger_entries") == entries_before + 2
    assert ledger_total(db) == 0


@pytest.mark.parametrize("htmx", [False, True])
def test_recipient_errors_are_generic(db, client, ids, htmx):
    token = logged_in_token(client)
    # Bob still has accounts, but none named Checking. Admin has no accounts.
    db.execute("UPDATE accounts SET name = 'Everyday' WHERE id = %s", (ids["bob"]["Checking"],))
    entries_before = count(db, "ledger_entries")
    transfers_before = count(db, "transfers")
    headers = {"HX-Request": "true"} if htmx else {}
    responses = [
        client.post("/ui/transfer", data=transfer_form(ids, token, to=email), headers=headers)
        for email in (
            "unknown@example.com",
            "another-unknown@example.org",
            "bob@example.com",
            "admin@example.com",
            "system@example.com",
            "system-funding@example.com",
            str(ids["system"]),
            "",
            " ",
            "bob",
            "bob@",
            "@example.com",
            "bob@@example.com",
            "bob @example.com",
        )
    ]
    for response in responses:
        assert response.status_code == 422
        assert '<p class="error" role="status">Recipient not found.</p>' in response.text
        assert ("<html" not in response.text) == htmx
        assert response.text == responses[0].text
    assert count(db, "ledger_entries") == entries_before
    assert count(db, "transfers") == transfers_before
    assert balance(db, ids["system"]) == -250_000
    assert ledger_total(db) == 0


def test_duplicate_checking_accounts_are_rejected(db, client, ids):
    token = logged_in_token(client)
    db.execute(
        "INSERT INTO accounts (user_id, name) SELECT user_id, name FROM accounts WHERE id = %s",
        (ids["bob"]["Checking"],),
    )
    entries_before = count(db, "ledger_entries")
    response = client.post("/ui/transfer", data=transfer_form(ids, token))
    assert response.status_code == 422
    assert "Recipient not found." in response.text
    assert count(db, "ledger_entries") == entries_before


@pytest.mark.parametrize("source", ["Checking", "Savings"])
def test_sending_to_own_email_is_rejected(db, client, ids, source):
    token = logged_in_token(client)
    entries_before = count(db, "ledger_entries")
    data = transfer_form(ids, token, to=" ALICE@EXAMPLE.COM ")
    data["from_account_id"] = str(ids["alice"][source])
    response = client.post("/ui/transfer", data=data)
    assert response.status_code == 422
    assert "Cannot send to yourself." in response.text
    assert count(db, "ledger_entries") == entries_before
    assert ledger_total(db) == 0


def test_numeric_destination_cannot_bypass_email_lookup(db, client, ids):
    token = logged_in_token(client)
    entries_before = count(db, "ledger_entries")
    data = transfer_form(ids, token)
    del data["to_email"]
    data["to_account_id"] = str(ids["bob"]["Checking"])
    response = client.post("/ui/transfer", data=data)
    assert response.status_code == 422
    assert "Recipient not found." in response.text
    assert count(db, "ledger_entries") == entries_before


def test_recipient_checking_currency_must_match_source(db, client, ids):
    token = logged_in_token(client)
    db.execute("UPDATE accounts SET currency = 'EUR' WHERE id = %s", (ids["bob"]["Checking"],))
    entries_before = count(db, "ledger_entries")
    response = client.post("/ui/transfer", data=transfer_form(ids, token))
    assert response.status_code == 422
    assert "Currency mismatch." in response.text
    assert count(db, "ledger_entries") == entries_before


def test_form_resubmission_with_same_key_moves_money_once(db, client, ids):
    token = logged_in_token(client)
    start = balance(db, ids["alice"]["Checking"])

    first = client.post("/ui/transfer", data=transfer_form(ids, token, key="dup"))
    second = client.post(
        "/ui/transfer", data=transfer_form(ids, token, key="dup", to=" BOB@EXAMPLE.COM ")
    )

    assert first.status_code == second.status_code == 200
    assert "already processed" in second.text
    assert balance(db, ids["alice"]["Checking"]) == start - 1_234
    assert ledger_total(db) == 0


def test_form_replay_after_draining_balance_moves_money_once(db, client, ids):
    token = logged_in_token(client)
    data = transfer_form(ids, token, amount="1000.00")
    assert client.post("/ui/transfer", data=data).status_code == 200
    entries_before = count(db, "ledger_entries")
    response = client.post("/ui/transfer", data=data)
    assert response.status_code == 200
    assert "already processed" in response.text
    assert balance(db, ids["alice"]["Checking"]) == 0
    assert count(db, "ledger_entries") == entries_before
    assert ledger_total(db) == 0


def test_form_key_conflict_does_not_move_money(db, client, ids):
    token = logged_in_token(client)
    assert client.post("/ui/transfer", data=transfer_form(ids, token)).status_code == 200
    entries_before = count(db, "ledger_entries")
    response = client.post("/ui/transfer", data=transfer_form(ids, token, amount="1.00"))
    assert response.status_code == 422
    assert "already used with a different request" in response.text
    assert count(db, "ledger_entries") == entries_before
    assert ledger_total(db) == 0


def test_page_overdraft_shows_an_error(db, client, ids):
    token = logged_in_token(client)
    entries_before = count(db, "ledger_entries")
    response = client.post("/ui/transfer", data=transfer_form(ids, token, amount="1000.01"))
    assert response.status_code == 422
    assert "Insufficient funds" in response.text
    assert balance(db, ids["alice"]["Checking"]) == 100_000
    assert count(db, "ledger_entries") == entries_before
    assert ledger_total(db) == 0


def test_page_rejects_bad_amounts(client, ids):
    token = logged_in_token(client)
    for amount in ("0", "-1", "abc", "1.234", "", "NaN"):
        response = client.post("/ui/transfer", data=transfer_form(ids, token, amount=amount))
        assert response.status_code == 422, amount


def test_login_page_error_is_generic(client):
    token = csrf_from_html(client.get("/ui/login").text)
    response = client.post(
        "/ui/login", data={"email": "alice@example.com", "password": "nope", "csrf_token": token}
    )
    assert response.status_code == 401
    assert "Invalid credentials" in response.text


# --- CSRF ------------------------------------------------------------------


def test_transfer_without_csrf_token_is_rejected(db, client, ids):
    logged_in_token(client)
    transfers_before = count(db, "transfers")
    start = balance(db, ids["alice"]["Checking"])

    data = transfer_form(ids, "")
    del data["csrf_token"]
    assert client.post("/ui/transfer", data=data).status_code == 403
    assert client.post("/ui/transfer", data=transfer_form(ids, "")).status_code == 403
    assert client.post("/ui/transfer", data=transfer_form(ids, "not-the-token")).status_code == 403

    assert count(db, "transfers") == transfers_before
    assert balance(db, ids["alice"]["Checking"]) == start


def test_csrf_token_from_another_session_is_rejected(db, ids):
    with (
        TestClient(app, base_url="http://localhost") as alice,
        TestClient(app, base_url="http://localhost") as bob,
    ):
        alice_token = logged_in_token(alice, "alice")
        bob_token = logged_in_token(bob, "bob")
        assert alice_token != bob_token

        # Bob's token on Alice's session.
        response = alice.post("/ui/transfer", data=transfer_form(ids, bob_token))
        assert response.status_code == 403
        assert balance(db, ids["alice"]["Checking"]) == 100_000


def test_logout_requires_csrf_token(client):
    token = logged_in_token(client)

    assert client.post("/ui/logout", data={}, follow_redirects=False).status_code == 403
    assert client.get("/ui/accounts").status_code == 200  # still logged in

    response = client.post("/ui/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/ui/accounts", follow_redirects=False).status_code == 303


def test_login_requires_csrf_token(client):
    form = {"email": "alice@example.com", "password": "test-password"}

    assert client.post("/ui/login", data=form, follow_redirects=False).status_code == 403

    client.get("/ui/login")  # receive the CSRF cookie
    wrong = client.post("/ui/login", data={**form, "csrf_token": "x.y"}, follow_redirects=False)
    assert wrong.status_code == 403
    assert not client.cookies.get("ll_session")


def test_forged_anonymous_login_token_is_rejected(client):
    # An attacker who can set the cookie and the field still cannot mint a valid token.
    client.cookies.set("ll_csrf", "attacker-value.deadbeef")
    response = client.post(
        "/ui/login",
        data={
            "email": "alice@example.com",
            "password": "test-password",
            "csrf_token": "attacker-value.deadbeef",
        },
        follow_redirects=False,
    )
    assert response.status_code == 403


# --- cookie attributes -----------------------------------------------------


def session_cookie_header(response) -> str:
    headers = response.headers.get_list("set-cookie")
    return next(h for h in headers if h.startswith("ll_session="))


def test_session_cookie_flags_on_localhost(client):
    header = session_cookie_header(page_login(client, "alice")).lower()
    assert "httponly" in header
    assert "samesite=strict" in header
    assert not re.search(r";\s*secure", header)


def test_session_cookie_is_secure_off_localhost():
    with TestClient(app, base_url="https://ledger.example") as client:
        header = session_cookie_header(page_login(client, "alice")).lower()
    assert "httponly" in header
    assert "samesite=strict" in header
    assert re.search(r";\s*secure", header)


def test_csrf_cookie_flags(client):
    response = client.get("/ui/login")
    header = next(h for h in response.headers.get_list("set-cookie") if h.startswith("ll_csrf="))
    assert "httponly" in header.lower()
    assert "samesite=strict" in header.lower()
