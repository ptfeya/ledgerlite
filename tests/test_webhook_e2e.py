"""Live end-to-end webhook flows through the real entry points.

The API runs under uvicorn on 127.0.0.1:8100 against its own
ledgerlite_webhooks_qa database, the worker runs as separate
`python -m app.webhook_worker` processes, and deliveries go to the real loopback
receiver on 127.0.0.1:8101. Child processes get a minimal environment (nothing
inherited except PATH) and a working directory without a .env file, and every
process started here is stopped even when a test fails.
"""

import json
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from uuid import UUID

import httpx
import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from app import webhooks
from app.db import apply_schema
from app.seed import seed, seed_admin
from app.webhook_worker import APPLICATION_NAME
from tests.conftest import ADMIN_DATABASE_URL, TEST_DATABASE_URL
from tests.helpers import balance, ledger_total
from tests.webhook_receiver import Reply

REPO_ROOT = Path(__file__).resolve().parent.parent
QA_DBNAME = "ledgerlite_webhooks_qa"
QA_DATABASE_URL = make_conninfo(TEST_DATABASE_URL, dbname=QA_DBNAME)
API_HOST = "127.0.0.1"
API_PORT = 8100
API_URL = f"http://{API_HOST}:{API_PORT}"
EVENT_KEYS = ["event_id", "type", "transfer_id", "amount_cents", "currency", "created_at"]
DELIVERED_LOG = re.compile(r"event ([0-9a-f-]{36}) delivered")

# Generated per run and never printed: synthetic credentials for the QA stack only.
QA_PASSWORD = secrets.token_urlsafe(24)
QA_JWT_SECRET = secrets.token_hex(32)
QA_WEBHOOK_SECRET = secrets.token_hex(32)


def readme_verify_signature():
    """The signature check documented in README.md, loaded from the README itself."""
    readme = (REPO_ROOT / "README.md").read_text()
    blocks = re.findall(r"```python\n(.*?)```", readme, re.DOTALL)
    assert len(blocks) == 1, "README.md should contain exactly one python example"
    namespace: dict = {}
    exec(compile(blocks[0], "README.md", "exec"), namespace)
    return namespace["verify_signature"]


def child_env(webhook_url: str | None) -> dict:
    """Only what the child needs. Developer DB URLs, webhook settings, tokens and
    other secrets in the parent environment are never passed on."""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": QA_DATABASE_URL,
        "JWT_SECRET": QA_JWT_SECRET,
        "SEED_PASSWORD": QA_PASSWORD,
    }
    if webhook_url is not None:
        env["WEBHOOK_URL"] = webhook_url
        env["WEBHOOK_SIGNING_SECRET"] = QA_WEBHOOK_SECRET
    return env


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((API_HOST, port)) == 0


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait(timeout=10)


def wait_until(predicate, timeout: float, message: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.05)


@pytest.fixture(scope="module")
def qa_database():
    """Recreate only the isolated QA database; dropped again afterwards."""

    def recreate(drop_only: bool = False) -> None:
        with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(QA_DBNAME))
            )
            if not drop_only:
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(QA_DBNAME)))

    recreate()
    with psycopg.connect(QA_DATABASE_URL) as conn:
        apply_schema(conn)
    yield
    recreate(drop_only=True)


@pytest.fixture(scope="module")
def live_api(qa_database, tmp_path_factory):
    """uvicorn on 127.0.0.1:8100, queueing events for the receiver on :8101."""
    if port_in_use(API_PORT):
        pytest.fail(f"port {API_PORT} is already in use; refusing to touch that process")
    workdir = tmp_path_factory.mktemp("live-api")
    log_path = workdir / "api.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                API_HOST,
                "--port",
                str(API_PORT),
            ],
            cwd=workdir,
            env=child_env(webhook_url="http://127.0.0.1:8101/hooks"),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        with httpx.Client(base_url=API_URL, trust_env=False, timeout=2) as probe:

            def ready() -> bool:
                if proc.poll() is not None:
                    raise AssertionError(f"API exited early:\n{log_path.read_text()[-2000:]}")
                try:
                    return probe.get("/accounts").status_code == 401
                except httpx.TransportError:
                    return False

            wait_until(ready, 30, "API did not become ready on 127.0.0.1:8100")
        yield proc
    finally:
        stop(proc)
    assert not port_in_use(API_PORT)


@pytest.fixture
def qa_db(live_api):
    """Fresh seeded QA data per test, and an autocommit connection for checks."""
    with psycopg.connect(QA_DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE ledger_entries, transfers, accounts, users RESTART IDENTITY CASCADE")
        seed(conn, QA_PASSWORD)
        seed_admin(conn, "admin@example.com", QA_PASSWORD)
    with psycopg.connect(QA_DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def api(qa_db):
    with httpx.Client(base_url=API_URL, trust_env=False, timeout=10) as client:
        yield client


@pytest.fixture
def worker(tmp_path):
    """Worker CLI processes against the QA database; all reaped at teardown."""
    procs: list[subprocess.Popen] = []

    def start(*args: str, url: str) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.webhook_worker", *args],
            cwd=tmp_path,
            env=child_env(webhook_url=url),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(proc)
        return proc

    def run_once(url: str) -> str:
        proc = start("--once", url=url)
        output, _ = proc.communicate(timeout=60)
        assert proc.returncode == 0, output
        return output

    start.run_once = run_once
    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


def login(api: httpx.Client, user: str) -> dict:
    response = api.post(
        "/auth/login", json={"email": f"{user}@example.com", "password": QA_PASSWORD}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def account_ids(db: psycopg.Connection) -> dict:
    rows = db.execute(
        "SELECT a.id, a.name, u.email FROM accounts a JOIN users u ON u.id = a.user_id"
    ).fetchall()
    return {(row["email"].split("@")[0], row["name"]): row["id"] for row in rows}


def transfer(api: httpx.Client, headers: dict, key: str, from_id: int, to_id: int, cents: int):
    return api.post(
        "/transfers",
        headers={**headers, "Idempotency-Key": key},
        json={"from_account_id": from_id, "to_account_id": to_id, "amount_cents": cents},
    )


def money(db: psycopg.Connection) -> tuple:
    return (
        db.execute("SELECT * FROM transfers ORDER BY created_at, id").fetchall(),
        db.execute("SELECT * FROM ledger_entries ORDER BY id").fetchall(),
    )


def outbox(db: psycopg.Connection) -> list[dict]:
    return db.execute("SELECT * FROM webhook_outbox ORDER BY created_at, event_id").fetchall()


def dead(db: psycopg.Connection) -> list[dict]:
    return db.execute("SELECT * FROM webhook_dead_letter ORDER BY failed_at").fetchall()


def test_readme_signature_example_verifies_exact_bytes_and_freshness():
    verify_signature = readme_verify_signature()
    body = b'{"event_id":"e","type":"transfer.completed"}'
    now = int(time.time())
    header = webhooks.signature("readme-test-secret", now, body)
    stale = webhooks.signature("readme-test-secret", now - 301, body)

    assert verify_signature("readme-test-secret", header, body) is True
    assert verify_signature("readme-test-secret", header, body + b" ") is False
    assert verify_signature("other-secret", header, body) is False
    assert (
        verify_signature("readme-test-secret", header.replace(f"t={now}", f"t={now + 1}"), body)
        is False
    )
    assert verify_signature("readme-test-secret", stale, body) is False
    malformed_headers = [
        "",
        "garbage",
        "t=,v1=",
        "v1=abc",
        f"t={now}",
        f"t=-{now},v1=00",
        f"t= {now},v1=00",
        "t=\u0661\u0662,v1=00",  # non-ASCII digits
        header[:-1] + "\u00e9",  # non-ASCII digest
        f"t={'9' * 400},v1=00",  # too large for a float
        f"t={'9' * 5000},v1=00",  # beyond the int parsing limit
    ]
    for malformed in malformed_headers:
        assert verify_signature("readme-test-secret", malformed, body) is False
    assert verify_signature("readme-test-secret", None, body) is False  # missing header


def test_live_transfer_is_delivered_signed_and_its_replay_adds_nothing(
    api, qa_db, receiver, worker
):
    verify_signature = readme_verify_signature()
    alice = login(api, "alice")
    ids = account_ids(qa_db)
    source, destination = ids[("alice", "Checking")], ids[("bob", "Checking")]

    response = transfer(api, alice, "e2e-delivery", source, destination, 2_500)

    assert response.status_code == 201
    assert "idempotent-replay" not in response.headers
    created = response.json()
    # Committed and visible to another connection before any worker runs.
    [stored_transfer] = [t for t in money(qa_db)[0] if t["user_id"] is not None]
    assert str(stored_transfer["id"]) == created["id"]
    entries = qa_db.execute(
        "SELECT account_id, amount_cents FROM ledger_entries WHERE transfer_id = %s",
        (stored_transfer["id"],),
    ).fetchall()
    assert sorted((e["account_id"], e["amount_cents"]) for e in entries) == sorted(
        [(source, -2_500), (destination, 2_500)]
    )
    assert (balance(qa_db, source), balance(qa_db, destination)) == (97_500, 77_500)
    assert ledger_total(qa_db) == 0
    [event] = outbox(qa_db)
    assert (event["status"], event["attempts"]) == ("pending", 0)
    assert receiver.requests == []  # the API itself never sends

    payload = json.loads(event["body"])
    assert list(payload) == EVENT_KEYS
    assert payload == {
        "event_id": str(event["event_id"]),
        "type": "transfer.completed",
        "transfer_id": created["id"],
        "amount_cents": 2_500,
        "currency": "USD",
        "created_at": webhooks.utc_timestamp(stored_transfer["created_at"]),
    }

    output = worker.run_once(receiver.url())

    assert f"event {event['event_id']} delivered" in output
    [request] = receiver.requests
    assert (request.method, request.path) == ("POST", "/hooks")
    assert request.headers["content-type"] == "application/json"
    assert request.body == event["body"].encode("utf-8")
    assert request.headers["idempotency-key"] == str(event["event_id"])
    header = request.headers["x-ledgerlite-signature"]
    assert re.fullmatch(r"t=\d+,v1=[0-9a-f]{64}", header)
    assert verify_signature(QA_WEBHOOK_SECRET, header, request.body) is True
    assert (
        verify_signature(QA_WEBHOOK_SECRET, header, request.body.replace(b"2500", b"2501")) is False
    )
    [delivered] = outbox(qa_db)
    assert (delivered["status"], delivered["attempts"], delivered["last_status_code"]) == (
        "delivered",
        1,
        200,
    )
    assert delivered["delivered_at"] is not None
    assert delivered["body"] == event["body"]

    # Same key, same request: the original transfer, no money, no new or reset event.
    before = (money(qa_db), outbox(qa_db))
    replay = transfer(api, alice, "e2e-delivery", source, destination, 2_500)

    assert replay.status_code == 201
    assert replay.headers["idempotent-replay"] == "true"
    assert replay.json() == created
    assert (money(qa_db), outbox(qa_db)) == before
    assert (balance(qa_db, source), balance(qa_db, destination)) == (97_500, 77_500)

    worker.run_once(receiver.url())
    assert len(receiver.requests) == 1  # a delivered event is never sent again


def test_two_live_workers_deliver_each_event_of_a_batch_once(api, qa_db, receiver, worker):
    concurrent = threading.Event()
    in_flight = 0
    lock = threading.Lock()

    def respond(request):
        nonlocal in_flight
        with lock:
            in_flight += 1
            if in_flight >= 2:
                concurrent.set()
        # Hold the first delivery (and its row lock) until the other worker has
        # skipped it and is sending a different event. Well under the worker's
        # read timeout, so a slow start cannot turn into a retry.
        concurrent.wait(8)
        with lock:
            in_flight -= 1
        return Reply(204)

    receiver.responder = respond
    workers = [worker("--poll-interval", "0.2", url=receiver.url()) for _ in range(2)]

    def both_connected() -> bool:
        for proc in workers:
            if proc.poll() is not None:
                raise AssertionError(f"worker exited early:\n{proc.communicate()[0]}")
        return (
            qa_db.execute(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE datname = %s AND application_name = %s",
                (QA_DBNAME, APPLICATION_NAME),
            ).fetchone()["n"]
            == 2
        )

    wait_until(both_connected, 30, "both workers should be connected and polling")

    alice = login(api, "alice")
    ids = account_ids(qa_db)
    for n in range(6):
        response = transfer(
            api,
            alice,
            f"e2e-batch-{n}",
            ids[("alice", "Checking")],
            ids[("bob", "Savings")],
            1_000 + n,
        )
        # Transfers proceed while a worker holds an outbox row lock.
        assert response.status_code == 201
    events = outbox(qa_db)
    assert len(events) == 6

    receiver.wait_for(6, timeout=30)
    time.sleep(1.0)  # several more polls by both workers
    outputs = []
    for proc in workers:
        proc.send_signal(signal.SIGTERM)
        outputs.append(proc.communicate(timeout=20)[0])

    assert [proc.returncode for proc in workers] == [0, 0], outputs
    assert concurrent.is_set()
    counts = Counter(json.loads(r.body)["event_id"] for r in receiver.requests)
    assert counts == Counter({str(e["event_id"]): 1 for e in events})
    stored = {str(e["event_id"]): e["body"].encode() for e in events}
    for request in receiver.requests:
        event_id = request.headers["idempotency-key"]
        assert request.body == stored[event_id]
        assert readme_verify_signature()(
            QA_WEBHOOK_SECRET, request.headers["x-ledgerlite-signature"], request.body
        )
    delivered_by = [set(DELIVERED_LOG.findall(output)) for output in outputs]
    assert all(delivered_by), "each worker should have delivered at least one event"
    assert delivered_by[0].isdisjoint(delivered_by[1])
    assert delivered_by[0] | delivered_by[1] == set(stored)
    assert {(e["status"], e["attempts"]) for e in outbox(qa_db)} == {("delivered", 1)}
    assert ledger_total(qa_db) == 0


def test_dead_letter_is_listed_replayed_and_redelivered_without_moving_money(
    api, qa_db, receiver, worker
):
    verify_signature = readme_verify_signature()
    alice = login(api, "alice")
    ids = account_ids(qa_db)
    source, destination = ids[("alice", "Checking")], ids[("bob", "Checking")]
    receiver.responder = lambda request: Reply(410)

    response = transfer(api, alice, "e2e-dead-letter", source, destination, 4_200)
    assert response.status_code == 201
    committed = money(qa_db)
    balances = (balance(qa_db, source), balance(qa_db, destination))
    [event] = outbox(qa_db)

    output = worker.run_once(receiver.url())

    assert "moved to dead letter after attempt 1 (HTTP 410)" in output
    [failed] = receiver.requests
    assert outbox(qa_db) == []
    [dead_row] = dead(qa_db)
    assert (dead_row["event_id"], dead_row["attempts"], dead_row["last_status_code"]) == (
        event["event_id"],
        1,
        410,
    )

    path = "/admin/webhooks/dead-letter"
    replay_path = f"{path}/{event['event_id']}/replay"
    assert api.get(path).status_code == 401
    assert api.get(path, headers=alice).status_code == 403
    assert api.post(replay_path, headers=alice).status_code == 403
    admin = login(api, "admin")

    listing = api.get(path, headers=admin)

    assert listing.status_code == 200
    [item] = listing.json()
    assert item["event_id"] == str(event["event_id"])
    assert item["transfer_id"] == response.json()["id"]
    assert (item["event_type"], item["attempts"], item["last_status_code"]) == (
        "transfer.completed",
        1,
        410,
    )
    assert "body" not in item

    replayed = api.post(replay_path, headers=admin)

    assert replayed.status_code == 200
    assert replayed.json()["event_id"] == str(event["event_id"])
    assert (replayed.json()["status"], replayed.json()["attempts"]) == ("pending", 0)
    assert api.post(replay_path, headers=admin).status_code == 404
    assert api.get(path, headers=admin).json() == []
    [requeued] = outbox(qa_db)
    assert (requeued["event_id"], requeued["body"]) == (event["event_id"], event["body"])

    receiver.responder = lambda request: Reply(200)
    output = worker.run_once(receiver.url())

    assert f"event {event['event_id']} delivered on attempt 1" in output
    first, second = receiver.requests
    assert first is failed
    assert second.body == failed.body == event["body"].encode("utf-8")
    assert second.headers["idempotency-key"] == failed.headers["idempotency-key"]
    assert second.headers["idempotency-key"] == str(UUID(json.loads(second.body)["event_id"]))
    assert verify_signature(
        QA_WEBHOOK_SECRET, second.headers["x-ledgerlite-signature"], second.body
    )
    [delivered] = outbox(qa_db)
    assert (delivered["event_id"], delivered["status"], delivered["attempts"]) == (
        event["event_id"],
        "delivered",
        1,
    )
    assert dead(qa_db) == []
    assert money(qa_db) == committed
    assert (balance(qa_db, source), balance(qa_db, destination)) == balances
    assert ledger_total(qa_db) == 0
