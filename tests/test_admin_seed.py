import pytest
from psycopg.errors import NotNullViolation

from app.config import get_settings
from app.db import apply_schema
from app.security import verify_password
from app.seed import main, seed
from tests.helpers import TEST_PASSWORD, count


@pytest.fixture
def seed_settings(monkeypatch):
    monkeypatch.setenv("SEED_PASSWORD", TEST_PASSWORD)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_schema_upgrades_existing_users_and_is_idempotent(db):
    # Simulate a database created before the admin flag existed.
    db.execute("ALTER TABLE users DROP COLUMN is_admin")
    apply_schema(db)

    assert all(not user["is_admin"] for user in db.execute("SELECT is_admin FROM users"))
    db.execute("UPDATE users SET is_admin = true WHERE email = 'admin@example.com'")
    apply_schema(db)
    apply_schema(db)

    roles = {
        row["email"]: row["is_admin"] for row in db.execute("SELECT email, is_admin FROM users")
    }
    assert roles == {
        "alice@example.com": False,
        "bob@example.com": False,
        "admin@example.com": True,
    }
    new_user = db.execute(
        "INSERT INTO users (email, password_hash) VALUES ('new@example.com', 'unused') "
        "RETURNING is_admin"
    ).fetchone()
    assert new_user["is_admin"] is False
    with pytest.raises(NotNullViolation):
        db.execute("UPDATE users SET is_admin = NULL WHERE email = 'new@example.com'")


@pytest.mark.parametrize("already_seeded", [False, True])
def test_seed_command_creates_admin_and_is_safe_to_repeat(
    db, monkeypatch, seed_settings, already_seeded
):
    if not already_seeded:
        db.execute("TRUNCATE ledger_entries, transfers, accounts, users RESTART IDENTITY CASCADE")
    monkeypatch.setattr("sys.argv", ["app.seed", "--admin-email", "operator@example.com"])
    main()
    users_before = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    counts_before = {
        table: count(db, table) for table in ("accounts", "transfers", "ledger_entries")
    }
    main()

    assert db.execute("SELECT * FROM users ORDER BY id").fetchall() == users_before
    assert {table: count(db, table) for table in counts_before} == counts_before
    admin = db.execute("SELECT * FROM users WHERE email = 'operator@example.com'").fetchone()
    assert admin["is_admin"] is True
    assert verify_password(admin["password_hash"], TEST_PASSWORD)
    assert (
        db.execute("SELECT 1 FROM accounts WHERE user_id = %s", (admin["id"],)).fetchone() is None
    )
    assert all(
        not row["is_admin"]
        for row in db.execute(
            "SELECT is_admin FROM users WHERE email IN ('alice@example.com', 'bob@example.com')"
        )
    )

    # A normal seed rerun must not grant or revoke privileges.
    assert seed(db, TEST_PASSWORD) is False
    assert db.execute("SELECT * FROM users ORDER BY id").fetchall() == users_before


def test_seed_command_promotes_existing_user_without_resetting_password(db, monkeypatch):
    alice_before = db.execute("SELECT * FROM users WHERE email = 'alice@example.com'").fetchone()
    counts_before = {
        table: count(db, table) for table in ("accounts", "transfers", "ledger_entries")
    }
    monkeypatch.setattr("sys.argv", ["app.seed", "--admin-email", " Alice@Example.COM "])
    monkeypatch.setenv("SEED_PASSWORD", "a-different-seed-password")
    get_settings.cache_clear()
    try:
        main()
    finally:
        get_settings.cache_clear()

    alice_after = db.execute("SELECT * FROM users WHERE email = 'alice@example.com'").fetchone()
    assert alice_after == {**alice_before, "is_admin": True}
    assert {table: count(db, table) for table in counts_before} == counts_before
