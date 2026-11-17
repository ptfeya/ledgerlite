"""Tests for tools/qa_harness.py: isolation guards, fixtures, outage and restart."""

import argparse
import json

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app.config import Settings
from tests.conftest import ADMIN_DATABASE_URL
from tests.helpers import TEST_PASSWORD, ledger_total
from tools import qa_harness as h

# --- guards (no database needed beyond conftest's autouse setup) ---------------------------


@pytest.fixture(autouse=True)
def _clean_libpq_env(monkeypatch):
    for name in ("FACTORY_API_KEY", "QA_EVIDENCE_TOKEN", "PGHOST", "PGHOSTADDR", "PGSERVICE"):
        monkeypatch.delenv(name, raising=False)


def test_target_is_derived_and_never_a_shared_database():
    for name in ("ledgerlite", "ledgerlite_test", "ledgerlite_qa", "ledgerlite_harness"):
        url = f"postgresql://u:p@localhost:5432/{name}"
        target = h.resolve_target(url)
        assert target.dbname == "ledgerlite_harness"
        assert target.role == "ledgerlite_harness_app"
        assert "dbname=postgres" in target.admin_url or "/postgres" in target.admin_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db.example.com:5432/ledgerlite",
        "postgresql://u:p@10.0.0.5/ledgerlite",
        "postgresql://u:p@localhost/ledgerlite?hostaddr=10.0.0.5",
    ],
)
def test_refuses_non_local_hosts(url):
    with pytest.raises(h.HarnessError, match="non-local"):
        h.resolve_target(url)


@pytest.mark.parametrize("name", ["FACTORY_API_KEY", "QA_EVIDENCE_TOKEN"])
def test_refuses_agent_credentials(name, monkeypatch):
    monkeypatch.setenv(name, "x")
    with pytest.raises(h.HarnessError, match="credentials"):
        h.resolve_target("postgresql://u:p@localhost/ledgerlite")


@pytest.mark.parametrize(
    ("name", "value"),
    [("PGHOST", "db.example.com"), ("PGHOSTADDR", "10.0.0.5"), ("PGSERVICE", "prod")],
)
def test_refuses_inherited_libpq_overrides(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(h.HarnessError, match="refusing"):
        h.resolve_target("postgresql:///ledgerlite")
    with pytest.raises(h.HarnessError, match="refusing"):
        h.resolve_target("postgresql://u:p@localhost/ledgerlite")


def test_refuses_unsafe_identifiers():
    with pytest.raises(h.HarnessError, match="unsafe"):
        h.resolve_target("postgresql://u:p@localhost/x%22%3B%20DROP%20DATABASE%20y")


def test_account_specs_require_integer_cents():
    assert h.parse_account_specs(["Checking=1500", "Sav=0"]) == [("Checking", 1500), ("Sav", 0)]
    for bad in ["Checking=12.50", "Checking=-5", "Checking", "=5", "A=1e3", "A=١٢"]:
        with pytest.raises(h.HarnessError):
            h.parse_account_specs([bad])
    with pytest.raises(h.HarnessError, match="duplicate"):
        h.parse_account_specs(["A=1", "A=2"])


# --- fixtures against the pytest database ------------------------------------------------------


def test_ensure_user_is_deterministic_and_idempotent(db, ids):
    from app.db import make_pool  # noqa: F401  (app import path sanity)

    with psycopg.connect(db.info.dsn, password=db.info.password, row_factory=dict_row) as conn:
        first = h.ensure_user(conn, "Carol@Example.com", TEST_PASSWORD, [("Main", 5_000)])
        again = h.ensure_user(conn, "carol@example.com", TEST_PASSWORD, [("Main", 5_000)])
        assert first == again
        account_id = first["accounts"]["Main"]["account_id"]
        assert h.balance_of(conn, account_id) == 5_000

        h.ensure_user(conn, "carol@example.com", TEST_PASSWORD, [("Main", 1_200)])
        assert h.balance_of(conn, account_id) == 1_200
        h.ensure_user(conn, "carol@example.com", TEST_PASSWORD, [("Main", 9_000)])
        assert h.balance_of(conn, account_id) == 9_000

    assert ledger_total(db) == 0
    assert db.execute("SELECT COUNT(*) AS n FROM accounts WHERE name = 'Main'").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM accounts WHERE is_system").fetchone()["n"] == 1
    unbalanced = db.execute(
        "SELECT transfer_id FROM ledger_entries GROUP BY transfer_id HAVING SUM(amount_cents) <> 0"
    ).fetchall()
    assert unbalanced == []


def test_ensure_user_keeps_password_and_only_promotes(db, client):
    with psycopg.connect(db.info.dsn, password=db.info.password, row_factory=dict_row) as conn:
        h.ensure_user(conn, "alice@example.com", "another-password", [], admin=True)
    ok = client.post("/auth/login", json={"email": "alice@example.com", "password": TEST_PASSWORD})
    assert ok.status_code == 200
    with psycopg.connect(db.info.dsn, password=db.info.password, row_factory=dict_row) as conn:
        h.ensure_user(conn, "alice@example.com", TEST_PASSWORD, [])
    row = db.execute("SELECT is_admin FROM users WHERE email = 'alice@example.com'").fetchone()
    assert row["is_admin"] is True


def test_reset_data_restores_standard_seed(db, ids):
    with psycopg.connect(db.info.dsn, password=db.info.password, row_factory=dict_row) as conn:
        h.ensure_user(conn, "carol@example.com", TEST_PASSWORD, [("Main", 700)])
        h.reset_data(conn, TEST_PASSWORD)
    emails = {r["email"] for r in db.execute("SELECT email FROM users").fetchall()}
    assert emails == {"alice@example.com", "bob@example.com", h.ADMIN_EMAIL}
    assert ledger_total(db) == 0


# --- full lifecycle on an isolated instance --------------------------------------------------


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(h, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(h, "LOG_PATH", tmp_path / "server.log")
    settings = Settings()
    url = make_conninfo(ADMIN_DATABASE_URL, dbname="ledgerlite_pytest")
    target = h.resolve_target(url)
    assert target.dbname == "ledgerlite_pytest_harness"
    try:
        yield target, settings
    finally:
        h.cmd_down(target)


def _login(port: int, email: str, password: str) -> int:
    import urllib.request
    from urllib.error import HTTPError

    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status
    except HTTPError as exc:
        return exc.code


def test_lifecycle_up_fixture_outage_restart_down(harness):
    target, settings = harness
    seed_pw = settings.seed_password

    first = h.cmd_up(target, settings, reset=False)
    assert first["changed"] is True and first["running"] is True
    again = h.cmd_up(target, settings, reset=False)
    assert again["changed"] is False

    state = h.load_state()
    assert oct(h.STATE_PATH.stat().st_mode & 0o777) == "0o600"
    port = state["port"]
    assert state["jwt_secret"] != settings.jwt_secret
    assert len(state["jwt_secret"]) >= 32
    assert _login(port, "alice@example.com", seed_pw) == 200

    conn = h._app_conn(state)
    h.ensure_user(conn, "dana@example.com", seed_pw, [("Checking", 4_200)])
    conn.close()
    assert _login(port, "dana@example.com", seed_pw) == 200

    h.set_outage(target, True)
    assert h.outage_active(target)
    assert _login(port, "alice@example.com", seed_pw) == 503
    h.set_outage(target, False)
    assert _login(port, "alice@example.com", seed_pw) == 200

    h.set_outage(target, True)
    with pytest.raises(h.HarnessError, match="outage"):
        h.cmd_restart(target, settings)
    assert h.server_alive(h.load_state())  # a refused restart leaves the app running
    h.set_outage(target, False)

    old_pid = state["pid"]
    restarted = h.cmd_restart(target, settings)
    new_state = h.load_state()
    assert restarted["running"] and new_state["pid"] != old_pid
    assert new_state["port"] == port
    assert new_state["jwt_secret"] == state["jwt_secret"]
    assert not h.server_alive({"pid": old_pid, "nonce": state["nonce"]})
    assert _login(port, "dana@example.com", seed_pw) == 200  # data survived

    h.cmd_down(target)
    result = h.cmd_down(target)  # idempotent
    assert result == {"server_stopped": False, "database_dropped": False}
    assert h.load_state() is None
    with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
        left = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target.dbname,)
        ).fetchone()
        role = admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (target.role,)).fetchone()
    assert left is None and role is None


def test_refuses_to_touch_database_without_marker(harness):
    target, settings = harness
    with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{target.dbname}"')
    with pytest.raises(h.HarnessError, match="not a harness database"):
        h.cmd_up(target, settings, reset=True)
    with pytest.raises(h.HarnessError, match="not a harness database"):
        h.drop_database(target)
    with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{target.dbname}"')


def test_commands_need_a_running_harness(harness, monkeypatch):
    with pytest.raises(h.HarnessError, match="not up"):
        h.run(argparse.Namespace(command="fixture", kind="reset"))


def test_refuses_existing_role_without_marker(harness):
    target, settings = harness
    with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE ROLE "{target.role}" LOGIN')
    try:
        with pytest.raises(h.HarnessError, match="not a harness role"):
            h.cmd_up(target, settings, reset=False)
        with pytest.raises(h.HarnessError, match="not a harness role"):
            h.cmd_down(target)
        with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
            assert admin.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (target.role,)
            ).fetchone()
    finally:
        with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP ROLE "{target.role}"')


def test_state_is_bound_to_the_configured_target(harness):
    target, settings = harness
    h.cmd_up(target, settings, reset=False)
    other = h.resolve_target(make_conninfo(ADMIN_DATABASE_URL, dbname="ledgerlite_other"))
    for call in (
        lambda: h.cmd_down(other),
        lambda: h.cmd_restart(other, settings),
        lambda: h.cmd_up(other, settings, reset=False),
    ):
        with pytest.raises(h.HarnessError, match="different database target"):
            call()
    assert h.server_alive(h.load_state())


def test_set_balance_waits_for_in_flight_transfer(db, ids):
    import threading

    account = ids["alice"]["Checking"]
    start = h.account_balance(db, account)
    holder = psycopg.connect(db.info.dsn, password=db.info.password, row_factory=dict_row)
    holder.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account,))
    transfer_id = holder.execute(
        "INSERT INTO transfers (user_id, idempotency_key, request_hash, from_account_id, "
        "to_account_id, amount_cents) VALUES (NULL, 'inflight', 'x', %s, %s, 50) RETURNING id",
        (account, ids["system"]),
    ).fetchone()["id"]
    holder.execute(
        "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
        "VALUES (%s, %s, -50), (%s, %s, 50)",
        (transfer_id, account, transfer_id, ids["system"]),
    )

    def adjust():
        with psycopg.connect(db.info.dsn, password=db.info.password, row_factory=dict_row) as c:
            h.set_balance(c, account, 0)
            c.commit()

    worker = threading.Thread(target=adjust)
    worker.start()
    worker.join(timeout=1)
    assert worker.is_alive()  # blocked on the account lock, not reading a stale balance
    holder.commit()
    holder.close()
    worker.join(timeout=10)
    assert h.account_balance(db, account) == 0
    assert start == 100_000
