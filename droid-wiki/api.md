# JSON API

Active contributors: ptfeya.

LedgerLite exposes six JSON endpoints without an `/api` prefix. Money amounts are integer cents. This is a source-derived reference, not a live API probe.

## Contract and authentication

Send JSON request bodies with `Content-Type: application/json`. Except for login, send `Authorization: Bearer <ACCESS_TOKEN>`. Session cookies do not authenticate API requests, even admin cookies. No CSRF field is required for these bearer-authenticated endpoints.

Missing, invalid, expired, or deleted-user credentials produce `401`, `{"detail":"Not authenticated"}`, and `WWW-Authenticate: Bearer`. User identity and admin status are read from the database on each authenticated request. Login normalizes email with `strip().lower()`.

Declared request-model, path, query, and header validation errors return FastAPI's `422` response with a `detail` array. Application errors use `{"detail":"<message>"}`. Input models use ordinary Pydantic types, not strict-integer models; callers should send JSON integers for IDs and cents.

`/docs`, `/redoc`, and `/openapi.json` are disabled and return `404`. Browser routes and the `/` redirect are documented in [Web interface](features/web-interface.md), not as JSON endpoints.

## Endpoints

| Method and path | Input | Success |
| --- | --- | --- |
| `POST /auth/login` | Required `email` and `password` strings, each at least one character | `200` token object |
| `GET /accounts` | Bearer token | `200` account array, ordered by account ID |
| `POST /transfers` | Bearer token, `Idempotency-Key`, transfer body | `201` transfer object, including replays |
| `GET /accounts/{account_id}/entries` | Bearer token; integer account ID; optional integer `limit`, default `100`, range `1..500` | `200` entry array, newest ID first |
| `GET /accounts/{account_id}/statement` | Bearer token; integer account ID | `200` statement object with up to 500 entries, newest ID first; **no ownership check** |
| `POST /reconcile` | Admin bearer token; no body required | `200` reconciliation object |

### Login

Request:

```json
{"email":"alice@example.com","password":"<SEED_PASSWORD>"}
```

Response:

```json
{"access_token":"<ACCESS_TOKEN>","token_type":"bearer"}
```

An unknown email or wrong password returns `401`, `Invalid credentials`, without distinguishing the two cases. Missing or empty fields return `422`; whitespace-only email is not rejected by the model but will fail authentication. Connection/pool failures handled by login return `503`, `Service temporarily unavailable`. Login does not set a browser session cookie.

### Accounts and entries

Each account object contains:

| Field | JSON type | Meaning |
| --- | --- | --- |
| `id` | integer | Account ID |
| `name` | string | Account label |
| `currency` | string | Stored currency code, seeded as `USD` |
| `balance_cents` | integer | Sum of that account's signed entries, zero when empty |

Only the caller's accounts are listed. There is no accounts-list pagination. The ownerless system account is not included.

Each entry contains `id` (integer), `transfer_id` (UUID string), `account_id` (integer), `amount_cents` (signed integer), and `created_at` (datetime string). Negative entries debit; positive entries credit. Entries for a missing account or another user's account both return `404`, `Account not found`. There is no cursor or offset parameter.

### Statement export

`GET /accounts/{account_id}/statement` takes a bearer token and an integer account ID. It has no query parameters and no response model. The handler in `app/routes/api.py` runs its own SQL against `ledger_entries` instead of calling `app/ledger.py`, and always returns the 500 newest entries by entry ID:

```json
{"account_id":2,"entries":[{"id":9,"transfer_id":"<UUID>","amount_cents":-2500,"created_at":"<DATETIME>"}]}
```

Entry objects omit `account_id`, unlike `/entries`. An account ID with no entries, including one that does not exist, returns `200` with an empty `entries` array rather than `404`.

The query filters only by account ID. It does not check that the caller owns the account or that the account is not the system funding account, so any authenticated user can read any account's recent entries. The `/entries` endpoint does perform that check. The docstring mentions a Statements page, but neither `app/routes/pages.py` nor `app/templates/` has one, and no test in `tests/` calls this endpoint. See [Security](security.md#known-gaps).
### Transfers and retries

Required body:

```json
{"from_account_id":2,"to_account_id":4,"amount_cents":2500}
```

IDs above are illustrative, not guaranteed seed IDs. `amount_cents` must be greater than zero and at most `1000000000000` (`10^12`). Account IDs are integers without an explicit positive-ID field bound.

`Idempotency-Key` is a required header with length `1..255`. The API does not strip whitespace from this header. Keys are scoped to the authenticated user.

Success returns `id` (UUID string), `from_account_id`, `to_account_id`, `amount_cents` (integers), and `created_at` (datetime string). The internal `replayed` field is not serialized. A matching retry returns the original body with status `201` and `Idempotent-Replay: true`; first execution omits that header.

| Status | `detail` | Condition |
| --- | --- | --- |
| `404` | `source account not found` | Source missing, system-owned, or not owned by caller |
| `404` | `destination account not found` | Destination missing or system-owned |
| `422` | `source and destination must differ` | Same account on both sides |
| `422` | `currency mismatch` | Different account currencies |
| `422` | `insufficient funds` | Source balance too low |
| `422` | `Idempotency-Key was already used with a different request` | Same user's key with a different source, destination, or amount |
| `422` | Validation `detail` array | Missing/invalid header, missing body field, invalid amount, or incompatible type |

The ledger also rejects out-of-range amounts directly with `amount must be a positive number of cents`; HTTP requests normally encounter model validation first. The API permits transfers between different accounts owned by the same user, unlike the UI's email-recipient flow.

An identical replay is resolved before checking the current balance, but amount, distinct-account, ownership, system-account, and currency checks still run first. A failed transaction does not reserve its key. See [Transfers](features/transfers.md) for transaction ordering.

### Reconciliation

A non-admin authenticated user receives `403`, `Admin access required`. Admin status is checked in the current `users` row, not trusted from JWT claims.

The endpoint reads the global sum of all ledger entries, including funding entries:

```json
{"status":"ok"}
```

If nonzero, it still returns `200`:

```json
{"status":"imbalance","imbalance_cents":123}
```

This is a read-only global check, not a repair operation or a complete per-transfer integrity check.

## Request examples

These commands are examples only. Replace every placeholder locally; do not put real credentials into documentation or shared logs.

```sh
curl -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  --data '{"email":"alice@example.com","password":"<SEED_PASSWORD>"}'

curl http://localhost:8000/accounts \
  -H 'Authorization: Bearer <ACCESS_TOKEN>'

curl -i -X POST http://localhost:8000/transfers \
  -H 'Authorization: Bearer <ACCESS_TOKEN>' \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: <UNIQUE_REQUEST_KEY>' \
  --data '{"from_account_id":2,"to_account_id":4,"amount_cents":2500}'

curl 'http://localhost:8000/accounts/2/entries?limit=100' \
  -H 'Authorization: Bearer <ACCESS_TOKEN>'

curl -X POST http://localhost:8000/reconcile \
  -H 'Authorization: Bearer <ADMIN_ACCESS_TOKEN>'
```

Repeat the transfer with the same token, key, and body to obtain a replay, not a second movement.

## Key source files and verification

Contracts: `app/routes/api.py`; identity: `app/auth.py`; transfer rules and ordering: `app/ledger.py`; disabled schema routes: `app/main.py`. Regression coverage includes `tests/test_access_control.py`, `tests/test_idempotency.py`, `tests/test_system_account.py`, `tests/test_login_errors.py`, `tests/test_ledger_zero_sum.py`, and `tests/test_api_docs_disabled.py`. Tests were inspected, not run for this documentation.

See [Security](security.md) and [Data model](reference/data-models.md) for the boundaries behind these responses.
