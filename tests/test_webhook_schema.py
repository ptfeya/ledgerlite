import psycopg
import pytest

from app.config import get_settings
from app.db import apply_schema
from app.seed import main
from tests.helpers import TEST_PASSWORD, count, dead_letters, outbox_events, post_transfer

QUEUE_TABLES = ("webhook_outbox", "webhook_dead_letter")


def _queue_snapshot(db) -> dict:
    return {
        "outbox": outbox_events(db),
        "dead": dead_letters(db),
        "bodies": db.execute(
            "SELECT event_id, convert_to(body, 'UTF8') AS raw FROM webhook_outbox "
            "UNION ALL SELECT event_id, convert_to(body, 'UTF8') FROM webhook_dead_letter "
            "ORDER BY event_id"
        ).fetchall(),
        "transfers": count(db, "transfers"),
        "entries": count(db, "ledger_entries"),
    }


def _dead_letter(db, event_id) -> None:
    with db.transaction():
        db.execute(
            "INSERT INTO webhook_dead_letter (event_id, event_type, transfer_id, body, attempts, "
            "last_error, created_at) SELECT event_id, event_type, transfer_id, body, 6, "
            "'connection_refused', created_at FROM webhook_outbox WHERE event_id = %s",
            (event_id,),
        )
        db.execute("DELETE FROM webhook_outbox WHERE event_id = %s", (event_id,))


@pytest.fixture
def queued(db, client, alice, ids, webhook_config):
    """One pending and one dead-lettered event created through the API."""
    webhook_config()
    source, destination = ids["alice"]["Checking"], ids["bob"]["Checking"]
    assert post_transfer(client, alice, "pending", source, destination, 100).status_code == 201
    assert post_transfer(client, alice, "dead", source, destination, 200).status_code == 201
    dead = db.execute(
        "SELECT o.event_id FROM webhook_outbox o JOIN transfers t ON t.id = o.transfer_id "
        "WHERE t.idempotency_key = 'dead'"
    ).fetchone()["event_id"]
    _dead_letter(db, dead)
    assert len(outbox_events(db)) == len(dead_letters(db)) == 1


def test_schema_reapply_keeps_queue_tables_and_events(db, queued):
    before = _queue_snapshot(db)

    apply_schema(db)
    apply_schema(db)

    assert _queue_snapshot(db) == before
    kinds = db.execute(
        "SELECT relname, relkind FROM pg_class WHERE relname = ANY(%s) ORDER BY relname",
        (list(QUEUE_TABLES),),
    ).fetchall()
    assert kinds == [
        {"relname": "webhook_dead_letter", "relkind": "r"},
        {"relname": "webhook_outbox", "relkind": "r"},
    ]
    indexes = {
        row["indexname"]
        for row in db.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = ANY(%s)", (list(QUEUE_TABLES),)
        )
    }
    assert {
        "webhook_outbox_pkey",
        "webhook_outbox_transfer_event_uq",
        "webhook_outbox_due_idx",
        "webhook_dead_letter_pkey",
        "webhook_dead_letter_transfer_event_uq",
        "webhook_dead_letter_failed_idx",
    } <= indexes
    foreign_keys = db.execute(
        "SELECT conrelid::regclass::text AS tbl, confrelid::regclass::text AS ref "
        "FROM pg_constraint WHERE contype = 'f' AND conrelid::regclass::text = ANY(%s) "
        "ORDER BY tbl",
        (list(QUEUE_TABLES),),
    ).fetchall()
    assert foreign_keys == [
        {"tbl": "webhook_dead_letter", "ref": "transfers"},
        {"tbl": "webhook_outbox", "ref": "transfers"},
    ]
    # Historical (seed funding) transfers are never backfilled.
    assert (
        db.execute(
            "SELECT COUNT(*) AS n FROM transfers t WHERE NOT EXISTS "
            "(SELECT 1 FROM webhook_outbox o WHERE o.transfer_id = t.id) AND NOT EXISTS "
            "(SELECT 1 FROM webhook_dead_letter d WHERE d.transfer_id = t.id)"
        ).fetchone()["n"]
        == 4
    )


def test_queue_constraints_reject_inconsistent_rows(db, queued):
    event = outbox_events(db)[0]
    funding = db.execute("SELECT id FROM transfers WHERE user_id IS NULL LIMIT 1").fetchone()["id"]
    insert = (
        "INSERT INTO webhook_outbox (event_id, event_type, transfer_id, body) "
        "VALUES (gen_random_uuid(), %s, %s, %s)"
    )

    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, ("transfer.completed", event["transfer_id"], "{}"))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(insert, ("transfer.completed", "00000000-0000-4000-8000-000000000000", "{}"))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(insert, ("transfer.completed", funding, "[1, 2]"))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(insert, ("transfer.updated", funding, "{}"))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE webhook_outbox SET status = 'delivered' WHERE event_id = %s",
            (event["event_id"],),
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "UPDATE webhook_outbox SET attempts = 7 WHERE event_id = %s", (event["event_id"],)
        )
    assert outbox_events(db) == [event]


@pytest.mark.parametrize("fresh", [True, False])
def test_seed_never_creates_webhook_events(db, monkeypatch, webhook_config, fresh):
    monkeypatch.setenv("SEED_PASSWORD", TEST_PASSWORD)
    webhook_config()
    assert get_settings().webhooks_enabled
    if fresh:
        db.execute("TRUNCATE ledger_entries, transfers, accounts, users RESTART IDENTITY CASCADE")
    monkeypatch.setattr("sys.argv", ["app.seed"])

    main()
    main()

    assert count(db, "transfers") == 4
    assert outbox_events(db) == dead_letters(db) == []


def test_transfer_truncate_cascade_clears_queues(db, queued):
    db.execute("TRUNCATE ledger_entries, transfers, accounts, users RESTART IDENTITY CASCADE")

    assert outbox_events(db) == dead_letters(db) == []


def test_outbox_delivery_state_has_a_single_stored_authority(db):
    columns = {
        r["column_name"]
        for r in db.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'webhook_outbox'"
        ).fetchall()
    }

    assert "status" in columns
    assert not columns & {"delivered_at", "updated_at"}
