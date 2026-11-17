"""The worker as operators run it: a separate `python -m app.webhook_worker` process."""

import hashlib
import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from app.ledger import execute_transfer
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import TEST_WEBHOOK_SECRET, dead_letters, outbox_events
from tests.webhook_receiver import Reply

REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT_SECRET = "test-only-current-webhook-secret"


def verify(secret: str, header: str, body: bytes) -> bool:
    parts = dict(item.split("=", 1) for item in header.split(","))
    expected = hmac.new(
        secret.encode(), parts["t"].encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def worker_env(url: str | None, secret: str | None) -> dict:
    """A minimal environment: nothing sensitive or webhook-related is inherited."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": TEST_DATABASE_URL,
        "JWT_SECRET": "test-only-secret-not-for-production",
    }
    if url is not None:
        env["WEBHOOK_URL"] = url
    if secret is not None:
        env["WEBHOOK_SIGNING_SECRET"] = secret
    return env


@pytest.fixture
def worker(tmp_path):
    """Start worker processes from a directory without a .env; always reaped."""
    procs: list[subprocess.Popen] = []

    def start(*args: str, url: str | None, secret: str | None = TEST_WEBHOOK_SECRET):
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.webhook_worker", *args],
            cwd=tmp_path,
            env=worker_env(url, secret),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        procs.append(proc)
        return proc

    def run(*args: str, url: str | None, secret: str | None = TEST_WEBHOOK_SECRET, timeout=30):
        proc = start(*args, url=url, secret=secret)
        output, _ = proc.communicate(timeout=timeout)
        return proc.returncode, output

    start.run = run
    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)


@pytest.fixture
def queue(db, pool, ids, webhook_config):
    alice = db.execute("SELECT id FROM users WHERE email = 'alice@example.com'").fetchone()["id"]
    created = 0

    def make(n: int, url: str, secret: str = TEST_WEBHOOK_SECRET) -> list[dict]:
        nonlocal created
        webhook_config(url=url, secret=secret)
        for _ in range(n):
            created += 1
            execute_transfer(
                pool,
                user_id=alice,
                idempotency_key=f"cli-{created}",
                from_account_id=ids["alice"]["Checking"],
                to_account_id=ids["bob"]["Checking"],
                amount_cents=1_000 + created,
            )
        return outbox_events(db)

    return make


def test_once_delivers_and_exits(db, queue, receiver, worker):
    events = queue(2, receiver.url())

    code, output = worker.run("--once", url=receiver.url())

    assert code == 0, output
    assert sorted(r.body for r in receiver.requests) == sorted(e["body"].encode() for e in events)
    for request in receiver.requests:
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        assert verify(TEST_WEBHOOK_SECRET, request.headers["x-ledgerlite-signature"], request.body)
        assert request.headers["idempotency-key"] == json.loads(request.body)["event_id"]
    assert [(e["status"], e["attempts"]) for e in outbox_events(db)] == [("delivered", 1)] * 2

    code, output = worker.run("--once", url=receiver.url())
    assert code == 0, output
    assert len(receiver.requests) == 2


def test_polling_worker_delivers_new_events_once_and_stops_on_sigterm(db, queue, receiver, worker):
    proc = worker("--poll-interval", "0.2", url=receiver.url())
    time.sleep(0.5)
    [event] = queue(1, receiver.url())

    [request] = receiver.wait_for(1)
    assert request.body == event["body"].encode()
    time.sleep(1.0)  # several more polls
    assert len(receiver.requests) == 1
    stored = outbox_events(db)[0]
    assert (stored["status"], stored["attempts"]) == ("delivered", 1)
    assert stored["delivered_at"] is not None

    proc.send_signal(signal.SIGTERM)
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode == 0, output


def test_two_live_workers_deliver_each_event_exactly_once(db, queue, receiver, worker):
    events = queue(8, receiver.url())
    concurrent = threading.Event()
    in_flight = 0
    lock = threading.Lock()

    def respond(request):
        nonlocal in_flight
        with lock:
            in_flight += 1
            if in_flight >= 2:
                concurrent.set()
        # The first request waits until the other worker skips its locked row
        # and sends a different event.
        concurrent.wait(15)
        time.sleep(0.05)
        with lock:
            in_flight -= 1
        return Reply(200)

    receiver.responder = respond

    first = worker("--once", url=receiver.url())
    second = worker("--once", url=receiver.url())
    outputs = [proc.communicate(timeout=60)[0] for proc in (first, second)]

    assert [first.returncode, second.returncode] == [0, 0], outputs
    assert concurrent.is_set()
    counts = Counter(json.loads(r.body)["event_id"] for r in receiver.requests)
    assert counts == Counter({str(e["event_id"]): 1 for e in events})
    for request in receiver.requests:
        assert request.headers["idempotency-key"] == json.loads(request.body)["event_id"]
    assert {(e["status"], e["attempts"]) for e in outbox_events(db)} == {("delivered", 1)}
    assert all("delivered" in output for output in outputs)


@pytest.mark.parametrize("url", [None, "", "   "])
@pytest.mark.parametrize("mode", [("--once",), ("--poll-interval", "0.2")])
def test_disabled_worker_sends_and_mutates_nothing(db, queue, receiver, worker, url, mode):
    queue(2, receiver.url())
    before = (outbox_events(db), dead_letters(db))

    code, output = worker.run(*mode, url=url, secret=None, timeout=15)

    assert code == 0, output
    assert "disabled" in output
    time.sleep(0.2)
    assert receiver.requests == []
    assert (outbox_events(db), dead_letters(db)) == before


def test_restarted_worker_uses_current_url_and_secret_for_queued_events(
    db, queue, receiver, worker
):
    events = queue(2, receiver.url("/old"), secret=TEST_WEBHOOK_SECRET)
    code, output = worker.run("--once", url="", secret=None)
    assert code == 0, output
    assert outbox_events(db) == events  # backlog kept while disabled

    code, output = worker.run("--once", url=receiver.url("/current"), secret=CURRENT_SECRET)

    assert code == 0, output
    assert receiver.paths() == ["/current", "/current"]
    for request in receiver.requests:
        header = request.headers["x-ledgerlite-signature"]
        assert verify(CURRENT_SECRET, header, request.body)
        assert not verify(TEST_WEBHOOK_SECRET, header, request.body)
    assert {e["status"] for e in outbox_events(db)} == {"delivered"}


def test_worker_output_and_state_expose_no_secret_signature_body_or_url(
    db, queue, receiver, worker
):
    marker = "response-marker-do-not-log"
    url_marker = "url-marker-do-not-log"
    receiver.responder = lambda request: Reply(
        500, headers={"X-Marker": marker}, body=marker.encode()
    )
    queue(1, receiver.url())

    outputs = []
    for url in [
        receiver.url(f"/hooks/{url_marker}?k={url_marker}"),
        f"http://127.0.0.1:9/{url_marker}",
    ]:
        _, output = worker.run("--once", url=url)
        outputs.append(output)
        db.execute("UPDATE webhook_outbox SET next_attempt_at = now()")
    failed_http, refused = outputs
    bad_config_code, bad_config = worker.run("--once", url=f"ftp://{url_marker}/")

    assert bad_config_code == 2
    [event] = outbox_events(db)
    assert event["attempts"] == 2
    assert (event["last_status_code"], event["last_error"]) == (None, "connection_refused")
    [request] = receiver.requests
    body = request.body.decode()
    signature = request.headers["x-ledgerlite-signature"]
    text = "\n".join([failed_http, refused, bad_config, json.dumps(event, default=str)])
    for needle in [marker, url_marker, TEST_WEBHOOK_SECRET, signature, signature[-64:], body]:
        assert needle not in text
    assert "HTTP 500" in failed_http
    assert "connection_refused" in refused
    assert "WEBHOOK_URL" in bad_config
