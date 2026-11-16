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

Open <http://localhost:8000>. Interactive API docs are at `/docs`.

Seeded users (password is `SEED_PASSWORD` from `.env`, default `ledgerlite-dev`; local development only):

| Email               | Accounts                                 |
| ------------------- | ---------------------------------------- |
| `alice@example.com` | Checking ($1,000.00), Savings ($500.00)  |
| `bob@example.com`   | Checking ($750.00), Savings ($250.00)    |

Opening balances are funded from a system account that no user owns, so the ledger sums to zero from the start.

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
curl -s -X POST localhost:8000/reconcile -H "Authorization: Bearer $TOKEN"
```

| Method and path               | Behavior                                                                                  |
| ----------------------------- | ----------------------------------------------------------------------------------------- |
| `POST /auth/login`            | `{email, password}` returns `{access_token, token_type}`. Bad credentials return 401.      |
| `GET /accounts`               | The caller's accounts with balances.                                                      |
| `POST /transfers`             | Moves money. Requires an `Idempotency-Key` header. See below.                              |
| `GET /accounts/{id}/entries`  | Ledger entries, newest first (`?limit=`, max 500). Someone else's account returns 404.     |
| `POST /reconcile`             | `{"status":"ok"}` or `{"status":"imbalance","imbalance_cents":N}`. Requires a token.       |

Account ids in the seeded data are `1` (system funding), `2`/`3` (Alice Checking/Savings), `4`/`5` (Bob Checking/Savings).

### Transfers

- Amounts are integer cents in USD. The source account must be yours. The destination can be any user account.
- A transfer writes a debit and a credit entry in one database transaction. An overdraft returns 422 and writes nothing.
- Repeating an `Idempotency-Key` with the same request returns the original response (same status and body, plus `Idempotent-Replay: true`) and moves no money. Reusing a key with a different request returns 422. Keys are scoped per user. A failed transfer does not consume its key.
- The system funding account has no owner and can never be a source or destination through the API.

## UI

Server-rendered pages under `/ui`: `/ui/login`, `/ui/accounts`, `/ui/transfer`, `/ui/history`.
The transfer form uses htmx and a per-form idempotency key, so a double submit moves money once.

Pages sign in with an `HttpOnly`, `SameSite=Strict` cookie (`Secure` everywhere except localhost).
Every form POST, including login and logout, requires a CSRF token. The JSON API ignores the cookie and accepts only the bearer header.

## Design notes

- Balances are never stored. They are the sum of an account's ledger entries.
- Transfers lock both accounts in id order, then check the balance, so concurrent transfers cannot overdraw and opposite transfers cannot deadlock.
- The database also enforces the invariants: a deferred trigger rejects entries for a transfer that do not sum to zero, and the ledger is append-only.
- Login error handling is deliberately minimal for now (generic 401, no rate limiting or lockout).

## Layout

```
app/            FastAPI app (routes/, templates/, static/, ledger.py, security.py, seed.py)
db/schema.sql   Tables, constraints and triggers (idempotent)
tests/          pytest suite (runs against the Compose Postgres)
```
