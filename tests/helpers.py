import re

import psycopg
from fastapi.testclient import TestClient

TEST_PASSWORD = "test-password"


def api_login(client: TestClient, user: str, password: str = TEST_PASSWORD) -> dict:
    response = client.post(
        "/auth/login", json={"email": f"{user}@example.com", "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def balance(db: psycopg.Connection, account_id: int) -> int:
    return db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS b FROM ledger_entries "
        "WHERE account_id = %s",
        (account_id,),
    ).fetchone()["b"]


def ledger_total(db: psycopg.Connection) -> int:
    return db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS t FROM ledger_entries"
    ).fetchone()["t"]


def count(db: psycopg.Connection, table: str) -> int:
    return db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608


def post_transfer(
    client: TestClient, headers: dict, key: str, from_id: int, to_id: int, cents: int
):
    return client.post(
        "/transfers",
        headers={**headers, "Idempotency-Key": key},
        json={"from_account_id": from_id, "to_account_id": to_id, "amount_cents": cents},
    )


def csrf_from_html(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "no csrf_token field in page"
    return match.group(1)


def page_login(client: TestClient, user: str, password: str = TEST_PASSWORD):
    """Log in through the HTML form, exactly as a browser would."""
    token = csrf_from_html(client.get("/ui/login").text)
    return client.post(
        "/ui/login",
        data={"email": f"{user}@example.com", "password": password, "csrf_token": token},
        follow_redirects=False,
    )
