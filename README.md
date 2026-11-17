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

### Webhook worker

With `WEBHOOK_URL` and `WEBHOOK_SIGNING_SECRET` set, transfers queue `transfer.completed`
events. A separate process delivers them (the API never sends webhooks itself):

```sh
uv run python -m app.webhook_worker          # poll until SIGTERM / Ctrl-C
uv run python -m app.webhook_worker --once   # one attempt per currently due event, then exit
```

Each POST carries the stored JSON body, `Idempotency-Key: <event_id>` and
`X-LedgerLite-Signature: t=<unix-seconds>,v1=<hex HMAC-SHA256 of "<t>." + body>`.
2xx is success; network errors, 429 and 5xx are retried with full-jitter backoff
(up to six recorded attempts); other responses, including redirects, are terminal and
move the event to `webhook_dead_letter`. The worker reads its configuration at startup;
restart it after changing either setting, which then also applies to queued events.
With `WEBHOOK_URL` empty it exits without sending or changing anything.
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
