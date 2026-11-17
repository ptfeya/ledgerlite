# Monitoring

LedgerLite currently relies on server output, a small authentication error log, and an admin-only ledger sum check. There is no custom metrics, distributed tracing, dashboard, or alerting configuration in the application (`app/main.py`, `app/auth.py`, `app/routes/api.py`).

## Logs

`authenticate` in `app/auth.py` logs database/pool outages through the module logger at error level:

```text
Authentication database unavailable (PoolTimeout)
```

The parenthesized value is the exception class, not the exception message. `OperationalError` and `InterfaceError` use the same pattern. The helper deliberately omits exception text and tracebacks because they can contain connection credentials. API and UI login return a generic 503 for these failures (`app/auth.py`, `app/routes/api.py`, `app/routes/pages.py`).

The development command in `README.md` starts Uvicorn in the terminal:

```sh
uv run uvicorn app.main:app --reload
```

Read that process's output for startup, shutdown, and HTTP request information. The repository does not configure a log sink, retention, JSON formatting, request IDs, or a central query tool in `app/main.py`. Do not assume authentication log handling also covers every database exception: token-user lookup in `app/auth.py` has no matching outage wrapper.

When adding logs, follow the existing exception-class-only pattern for connection failures. Do not log passwords, JWTs, CSRF tokens, database URLs, or raw request bodies.

## Database and process readiness

The Compose database health check runs `pg_isready` every two seconds with a three-second timeout and 30 retries (`docker-compose.yml`). Inspect it without changing database state:

```sh
docker compose ps
docker compose logs db
```

This checks Postgres readiness, not schema application or ledger correctness. Application startup opens the pool with a 10-second wait in `app/main.py`; schema application belongs to `app/db.py` and `app/seed.py`.

There is no dedicated application health endpoint in `app/routes/api.py` or `app/routes/pages.py`. An HTTP response from `/ui/login` shows that the server answered, but that GET does not query Postgres and is not a database health check.

## Reconciliation

An admin bearer token can call `POST /reconcile` in `app/routes/api.py`. `ledger_imbalance` in `app/ledger.py` sums every signed ledger entry:

| Response | Meaning |
| --- | --- |
| `{"status":"ok"}` | The global sum is zero. |
| `{"status":"imbalance","imbalance_cents":N}` | The global sum is nonzero. |

Both result bodies are normal 200 responses. Monitor the body rather than HTTP status alone. The endpoint does not repair entries, prove per-transfer correctness, or report overdrafts. Do not bypass the append-only trigger to “fix” an imbalance (`app/routes/api.py`, `app/ledger.py`, `db/schema.sql`).

## QA evidence is separate

The QA server writes a lifecycle record under `qa-results/`; its verifier checks process identity, checkout fingerprint, and an HTTP nonce. That is a local freshness check, not production monitoring or proof of trusted code (`.factory/skills/qa/scripts/qa-server.sh`).

See [Debugging](how-to-contribute/debugging.md) for symptom-driven checks, [JSON API](api.md) for reconciliation authentication, and [Deployment](deployment.md) for lifecycle boundaries.
