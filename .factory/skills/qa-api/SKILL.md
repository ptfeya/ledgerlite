---
name: qa-api
description: >
  QA tests for the LedgerLite JSON API (bearer-token auth): login, accounts, transfers,
  entries, idempotency, overdraft, and admin-only reconcile. Driven with curl.
---

# QA: LedgerLite JSON API

Describes WHAT to verify. The orchestrator picks only flows relevant to the diff.

## Testing Target

Test the checked-out branch, never a remote deployment.

1. Ensure Postgres is up and `DATABASE_URL`, `JWT_SECRET`, `SEED_PASSWORD` are set (CI provides them). Locally, load `.env` with `set -a; . ./.env; set +a` and never `cat`, echo, or print it or its values.
2. Follow the orchestrator's Step 4: run `.factory/skills/qa/scripts/qa-server.sh verify` and reuse the server only if it passes. Otherwise run `qa-server.sh start` with the Execute tool's `fireAndForget: true` (a shell `&` is killed when the call returns), then poll `verify`. It uses the throwaway `<db>_qa` database and seeds the admin persona.
3. Base URL: the output of `qa-server.sh url` (normally `http://localhost:8000`). Routes have no `/api` prefix.

## Authentication

Credentials come from env (`SEED_PASSWORD`); never print them or tokens. `POST /auth/login` with JSON `{"email","password"}` returns `access_token`; send it as `Authorization: Bearer <token>`. Personas: member (alice), counterparty (bob), admin (qa-admin@example.com).

## Flows (menu)

- **api-login**: valid login returns a token; wrong password returns 401; empty fields return 422.
- **api-accounts**: `GET /accounts` lists only the caller's accounts with integer `balance_cents`. No token returns 401.
- **api-transfer-happy**: `POST /transfers` with `Idempotency-Key` header and integer `amount_cents` returns 201. Sender balance drops and recipient rises by exactly the amount; total across accounts is unchanged.
- **api-idempotency**: repeat the same key and body: same transfer id, `Idempotent-Replay: true`, balances change once. Same key with a different body must be rejected, not applied.
- **api-overdraft**: amount above balance is rejected with a 4xx; balances unchanged.
- **api-validation**: `amount_cents` of 0, negative, a float, or above the max returns 422; missing `Idempotency-Key` returns 422; same source and destination account is rejected.
- **api-funding-account**: the system funding account (no owner) is rejected as source and as destination.
- **api-ownership**: transferring from another user's account is rejected; `GET /accounts/{id}/entries` for another user's account returns 404.
- **api-entries**: entries for a transfer net to zero (one debit, one credit); `limit` bounds enforced (1 to 500).
- **api-cookie-rejected**: log in via `/ui/login`, then call `GET /accounts` with only the session cookie. Must return 401.
- **api-reconcile**: admin gets `{"status":"ok"}` after normal transfers; member gets 403; no token gets 401.

## Per-persona variations

Run ownership and reconcile checks as member, counterparty, and admin. Admin must still not be able to use the funding account as a transfer endpoint.

## Known Failure Modes

1. **Port 5432 taken or Postgres not running.** Server fails at startup or login returns 503. Start `docker compose up -d --wait` or point `DATABASE_URL` at the CI service.
2. **Seeded user missing.** Login 401 for alice/bob means seed was not run against the same database.
3. **Stale state.** Balances differ from seed after earlier runs. Re-seeding does not reset it. Restart with `qa-server.sh start`, which recreates the QA database, before comparing absolute balances; prefer asserting deltas.
