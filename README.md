# LedgerLite

A small payments and transfers service built on a double-entry ledger.

Python 3.12 · uv · FastAPI · Postgres 16 (Docker Compose) · psycopg · server-rendered HTML with htmx · pytest

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you)
- Docker with Compose v2

## Setup

```sh
cp .env.example .env            # then set JWT_SECRET (see below)
docker compose up -d --wait     # Postgres 16 on 127.0.0.1:5432
uv sync                         # install dependencies into .venv
uv run python -m app.seed       # create tables and seed demo data (safe to re-run)
```

`JWT_SECRET` is required and the app will not start without it (16+ characters).
The value in `.env.example` is a placeholder; generate your own:

```sh
openssl rand -hex 32
```

If port 5432 is taken, change `POSTGRES_PORT` **and** the port in `DATABASE_URL` in `.env`.

## Run

```sh
uv run uvicorn app.main:app --reload
```

Open <http://localhost:8000>. Interactive API docs (`/docs`, `/redoc`, `/openapi.json`) are disabled; the API is described in the [API](#api) section below.

Seeded users (password is `SEED_PASSWORD` from `.env`, default `ledgerlite-dev`; local development only):

| Email               | Accounts                                 |
| ------------------- | ---------------------------------------- |
| `alice@example.com` | Checking ($1,000.00), Savings ($500.00)  |
| `bob@example.com`   | Checking ($750.00), Savings ($250.00)    |

Opening balances are funded from a system account that no user owns, so the ledger sums to zero from the start.

### Create an admin

All users, including Alice, Bob, and users in an existing database, default to non-admin.
Explicitly create an admin for local development:

```sh
uv run python -m app.seed --admin-email admin@example.com
```

This also works on an already seeded database. A new admin uses `SEED_PASSWORD` and has
no accounts. If the email already exists, the command promotes that user without
changing their password or accounts. Re-running it is safe. Seeding without
`--admin-email` does not grant or revoke admin access.

## Test

```sh
uv run pytest
```

Tests need the Compose Postgres running. They create and drop a separate `<POSTGRES_DB>_test` database
(`ledgerlite_test` by default) and never touch the development data.

`tests/test_webhook_e2e.py` also runs the real stack: the API on `127.0.0.1:8100` against its own
`ledgerlite_webhooks_qa` database, worker processes, and a receiver on `127.0.0.1:8101`. Both
ports must be free. Run it alone with `uv run pytest tests/test_webhook_e2e.py`.

Lint and format:

```sh
uv run ruff check .
uv run ruff format .
```

CI (`.github/workflows/ci.yml`) starts Postgres and runs ruff and pytest on every push and pull request.

## API

Authenticate, then send the token as `Authorization: Bearer <token>`.

```sh
TOKEN=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"ledgerlite-dev"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')

curl -s localhost:8000/accounts -H "Authorization: Bearer $TOKEN"

curl -s localhost:8000/transfers -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H "Idempotency-Key: $(uuidgen)" \
  -d '{"from_account_id": 2, "to_account_id": 4, "amount_cents": 2500}'

curl -s localhost:8000/accounts/2/entries -H "Authorization: Bearer $TOKEN"

# Reconcile requires an admin token (create the admin as documented above).
ADMIN_TOKEN=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@example.com","password":"ledgerlite-dev"}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["access_token"])')
curl -s -X POST localhost:8000/reconcile -H "Authorization: Bearer $ADMIN_TOKEN"
```

| Method and path               | Behavior                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------- |
| `POST /auth/login`            | `{email, password}` returns `{access_token, token_type}`. Bad credentials return 401.      |
| `GET /accounts`               | The caller's accounts with balances.                                                      |
| `POST /transfers`             | Moves money. Requires an `Idempotency-Key` header. See below.                              |
| `GET /accounts/{id}/entries`  | Ledger entries, newest first (`?limit=`, max 500). Someone else's account returns 404.     |
| `POST /reconcile`             | `{"status":"ok"}` or `{"status":"imbalance","imbalance_cents":N}`. Requires an admin token; non-admins get 403, missing or invalid tokens get 401. |
| `GET /admin/webhooks/dead-letter` | Dead-letter webhook events, oldest failure first: id, type, transfer id, attempts, last HTTP status or error label, timestamps. At most 100 per response (`?limit=` 1-100); bodies are not returned. Admin token only, as for reconcile. |
| `POST /admin/webhooks/dead-letter/{event_id}/replay` | Moves that event back to the outbox, due now, with zero attempts and its original id and body. Never moves money. Returns the queued event; 404 if it is not in the dead letter (unknown, queued, delivered or already replayed), 409 if the outbox already holds an event for its transfer. Admin token only. |

Account ids in the seeded data are `1` (system funding), `2`/`3` (Alice Checking/Savings), `4`/`5` (Bob Checking/Savings).

### Transfers

- Amounts are integer cents in USD. The source account must be yours. The destination can be any user account.
- A transfer writes a debit and a credit entry in one database transaction. An overdraft returns 422 and writes nothing.
- Repeating an `Idempotency-Key` with the same request returns the original response (same status and body, plus `Idempotent-Replay: true`) and moves no money. Reusing a key with a different request returns 422. Keys are scoped per user. A failed transfer does not consume its key.
- The system funding account has no owner and can never be a source or destination through the API.

## UI

Server-rendered pages under `/ui`: `/ui/login`, `/ui/accounts`, `/ui/transfer`, `/ui/history`.
The transfer form uses htmx and a per-form idempotency key, so a double submit moves money once.
Enter the recipient's email, not an account ID. Email matching ignores case and surrounding
whitespace and resolves to the recipient's Checking account. Sending to yourself is rejected,
and the Checking account's currency must match the source. After form validation, all business
failures (including unknown recipients, overdrafts, currency mismatches and key conflicts)
return the same generic 422 response, for both full pages and htmx fragments. A failed
transfer never moves money or consumes its idempotency key. The JSON API still uses account IDs.

`POST /ui/transfer` allows 20 CSRF-valid attempts per user per 60-second fixed window,
including failures and replays. Further attempts return 429 with `Retry-After`, without
looking up the recipient or executing a transfer. Counters live in Postgres, so new sessions,
app workers and app restarts do not reset them. Reapply the schema with
`uv run python -m app.seed` when upgrading an existing database. A successful transfer still
confirms that the recipient exists; throttling limits probing but cannot hide completed money movement.

Pages sign in with an `HttpOnly`, `SameSite=Strict` cookie (`Secure` everywhere except localhost).
Every form POST, including login and logout, requires a CSRF token. The JSON API ignores the cookie and accepts only the bearer header.

## Webhooks

LedgerLite can notify another service whenever a transfer completes. Each new transfer made
through the API or the UI queues one `transfer.completed` event in the `webhook_outbox` table,
in the same database transaction as its ledger entries: no transfer without its event, no event
without its transfer. A separate worker process delivers queued events. The API never sends
webhooks itself, so a slow or unreachable receiver cannot delay or fail a transfer.

### Enable

Set both values in `.env`, then restart the app and the worker. Both read their settings once,
at startup.

```sh
WEBHOOK_URL=https://receiver.example.com/ledgerlite
WEBHOOK_SIGNING_SECRET=...   # generate with: openssl rand -hex 32
```

- `WEBHOOK_URL` must be an absolute `http://` or `https://` URL. Use HTTPS for anything other
  than localhost; certificates and hostnames are verified. Percent-encode any non-ASCII
  characters in the path or query (`/caf%C3%A9`, not `/café`); the URL is sent exactly as
  configured. `WEBHOOK_SIGNING_SECRET` is required
  whenever `WEBHOOK_URL` is set; the app and worker refuse to start without it.
- On an existing database, create the webhook tables with `uv run python -m app.seed`
  (safe to re-run).
- Only transfers made while webhooks are enabled queue events. Earlier transfers and the seeded
  opening balances are never backfilled. Repeating a transfer's `Idempotency-Key` returns the
  original transfer and never queues or resets an event.
- Leaving `WEBHOOK_URL` empty or unset disables webhooks: new transfers queue nothing and the
  worker exits without sending anything. Events that are already queued stay in the outbox,
  unchanged, until webhooks are enabled again.
- The worker always signs and sends with the current `WEBHOOK_URL` and
  `WEBHOOK_SIGNING_SECRET`, including for events queued earlier and pending retries. Changing the
  URL redirects the whole backlog; rotating the secret signs every later attempt with the new one.

### Run the worker

```sh
uv run python -m app.webhook_worker                     # poll every 5 s until SIGTERM / Ctrl-C
uv run python -m app.webhook_worker --poll-interval 1   # poll every second
uv run python -m app.webhook_worker --once              # one attempt per event due now, then exit
```

The polling worker sleeps between polls without holding a database connection. `--once` suits
cron jobs and manual runs: it attempts each event that is due when it starts at most once, then
exits. Exit status is 0 on success or when webhooks are disabled, 1 if a delivery transaction
failed (`--once` only), and 2 for invalid configuration.

You can run several workers at once. Each locks the event it is sending
(`FOR UPDATE SKIP LOCKED`) until the outcome is saved, so healthy workers never send the same
event at the same time. Worker logs show event ids and an HTTP status or error label, never
URLs, secrets, signatures or response bodies.

### Requests

```text
POST <WEBHOOK_URL>
Content-Type: application/json
Idempotency-Key: 0b9c6f9e-5d0a-4a43-9f1e-3c2d1b0a9e8f
X-LedgerLite-Signature: t=1767323045,v1=<64 lowercase hex characters>

{"event_id":"0b9c6f9e-5d0a-4a43-9f1e-3c2d1b0a9e8f","type":"transfer.completed","transfer_id":"5f3e1c2a-7b4d-4e6f-8a9b-0c1d2e3f4a5b","amount_cents":2500,"currency":"USD","created_at":"2026-01-02T03:04:05.123456Z"}
```

The body has exactly these six fields. `amount_cents` is an integer and `created_at` is the
transfer's time in UTC. The body is stored when the transfer commits and sent byte for byte on
every attempt and replay, so `event_id`, the body and `Idempotency-Key` (always the `event_id`)
never change. `t` is the Unix time of each attempt, so the signature differs between attempts.

### Verify signatures

`v1` is the lowercase hex HMAC-SHA256, keyed with `WEBHOOK_SIGNING_SECRET`, of the ASCII
timestamp, a `.`, and the raw request body. Verify the raw bytes you received before parsing the
JSON (parsing and re-serializing changes the bytes), compare digests in constant time, and reject
stale timestamps:

```python
import hashlib
import hmac
import time


def verify_signature(secret: str, header: str, body: bytes, tolerance: int = 300) -> bool:
    """Check an X-LedgerLite-Signature header against the raw request body."""
    if not isinstance(header, str):  # e.g. a missing header passed as None
        return False
    try:
        fields = dict(part.split("=", 1) for part in header.split(","))
        timestamp, received = fields["t"], fields["v1"]
    except (KeyError, ValueError):
        return False
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        return False
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    signed = timestamp.encode("ascii") + b"." + body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), received.encode())
```

`tests/test_webhook_e2e.py` runs this exact example against real deliveries.

### Retries and the dead letter

- **2xx:** delivered. The event stays in the outbox, marked `delivered`, and is not sent again.
- **Network errors (refused, timeout, DNS, TLS), 429 and 5xx:** retried. After failed attempt
  `n` (1 to 5), the next attempt is due after a random delay drawn uniformly from
  `[0, min(300, 5 * 2**(n - 1))]` seconds ("full jitter": at most 5, 10, 20, 40 and 80 seconds).
  The due time is stored, so restarting the worker does not retry early.
- **Anything else is terminal:** other 4xx responses (408 included) and 3xx redirects, which are
  never followed.
- Each request is a single attempt with bounded timeouts: 5 s to connect, 10 s per read and
  15 s for the whole request (DNS lookups are bounded only by the system resolver). There are no
  hidden client retries, and response bodies are never read.
- After the sixth recorded failed attempt, or right after a terminal response, the event moves
  to `webhook_dead_letter` with its attempt count, last HTTP status or error label, and failure
  time. Dead-letter events are not retried.

An attempt counts once its outcome is saved. If a worker dies mid-attempt, that attempt is not
counted and the event is simply sent again later.

Fix the receiver, then replay dead-letter events with an admin token (`ADMIN_TOKEN` from the
[API](#api) example):

```sh
curl -s localhost:8000/admin/webhooks/dead-letter -H "Authorization: Bearer $ADMIN_TOKEN"
curl -s -X POST localhost:8000/admin/webhooks/dead-letter/<event_id>/replay \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

A replay moves the event back to the outbox, due immediately, with a fresh budget of six
attempts and its original `event_id` and body. A running worker picks it up on its next poll, or
run `uv run python -m app.webhook_worker --once`. Replays never touch transfers or the ledger.
Replaying an event that is not in the dead letter (unknown, queued, delivered or already
replayed) returns 404. Ordinary users get 403; missing or invalid tokens get 401.

### At-least-once delivery

Events are delivered at least once, not exactly once. If a worker crashes, loses its database
connection or times out after the receiver accepted a request but before the outcome is saved,
the same event is sent again. A replay can also resend an event the receiver already handled.
Receivers should:

- deduplicate on `event_id` (also sent as `Idempotency-Key`), recording it in the same
  transaction as their own side effects, for example under a unique constraint;
- verify the signature and timestamp before acting on a request;
- respond with 2xx quickly and do slow work afterwards, so requests do not time out.
## Design notes

- Balances are never stored. They are the sum of an account's ledger entries.
- Transfers lock both accounts in id order, then check the balance, so concurrent transfers cannot overdraw and opposite transfers cannot deadlock.
- The database also enforces the invariants: a deferred trigger rejects entries for a transfer that do not sum to zero, and the ledger is append-only.
- Unknown emails and wrong passwords return the same generic 401 error.
- Login database/pool failures return a generic 503 and log only the exception type.
- Missing or empty login fields return 422: inline HTML for the UI, JSON for the API.
- Login rate limiting and lockout are not implemented.

## Layout

```
app/            FastAPI app (routes/, templates/, static/, ledger.py, security.py, seed.py)
db/schema.sql   Tables, constraints and triggers (idempotent)
tests/          pytest suite (runs against the Compose Postgres)
```
