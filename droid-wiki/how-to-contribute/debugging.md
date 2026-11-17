# Debugging

Start with configuration and the correct authentication transport, then inspect the affected route and ledger operation. Do not reset persistent development data, disable ledger triggers, or log credentials to diagnose a failure.

## Startup and database access

| Symptom | Check and next action |
| --- | --- |
| Settings validation fails | `app/config.py` requires `JWT_SECRET` with at least 16 characters. Set a real local value; do not print `.env`. Settings are cached, so restart the running process after configuration changes. |
| Postgres port conflict or connection failure | Compare `POSTGRES_PORT` with the port in `DATABASE_URL`; changing Compose alone does not rewrite the application URL. Inspect `docker compose ps` and `docker compose logs db`. |
| Application fails during lifespan | `app/main.py` waits up to 10 seconds opening the pool. Verify Postgres readiness and the URL/credentials before investigating routes. |
| Missing tables/users | Startup opens a pool but does not apply schema. Run the documented seed command against the intended local database. Re-seeding skips demo creation if any user exists. |
| Tests fail before an apparently mocked test | Session/function-autouse fixtures still recreate and seed the derived `_test` database. Check administration privileges and the actual base URL. |

Sources: `docker-compose.yml`, `app/config.py`, `app/main.py`, `app/db.py`, `app/seed.py`, `tests/conftest.py`. See [Configuration](../reference/configuration.md) and [Testing](testing.md).

## Login, cookies, and authorization

- **401 at login:** unknown email and wrong password deliberately return the same error. Ensure the intended database was seeded and use the password present when the user was created. Changing `SEED_PASSWORD` does not change existing password hashes (`app/auth.py`, `app/seed.py`).
- **422 at login:** missing/empty fields are validation failures. The UI checks CSRF first, so a request missing both credentials and CSRF returns 403 instead (`app/routes/api.py`, `app/routes/pages.py`).
- **503 at login:** `OperationalError`, `InterfaceError`, and `PoolTimeout` become a generic unavailable response. The `app.auth` error log records only the exception class; do not add raw messages or tracebacks containing credentials (`app/auth.py`, `tests/test_login_errors.py`).
- **401 from JSON after browser login:** expected without `Authorization: Bearer …`. `ll_session` authenticates HTML only. API routes have no `/api` prefix (`app/auth.py`, `app/routes/api.py`).
- **Repeated UI redirects to login:** check session expiry and hostname consistency. Cookies are HttpOnly and SameSite Strict; Secure is omitted only for `localhost`, `127.0.0.1`, and `::1`. Plain HTTP on another hostname will not behave like local development (`app/security.py`).
- **403 from `/reconcile`:** the bearer user must currently be admin in the database. Create/promote a local admin explicitly through the seed command; Alice and Bob are not admins by default (`app/auth.py`, `app/seed.py`).
- **404 for `/docs`, `/redoc`, or `/openapi.json`:** intentional, not a routing outage (`app/main.py`).

## CSRF failures

For login, GET `/ui/login` first and submit its hidden token with the matching cookie. Matching arbitrary cookie/field strings are insufficient: the anonymous token must also have a valid signature.

For transfer and logout, obtain the token from a page in the same authenticated session. Tokens from another session fail. Re-fetch the page after signing in again rather than reusing stale forms. Never bypass CSRF for debugging or put real token values in evidence.

Sources: `app/routes/pages.py`, `app/security.py`, `tests/test_pages_and_csrf.py`. See [Security](../security.md).

## Transfers and history

| Response or observation | Interpretation |
| --- | --- |
| 422 invalid amount | API accepts positive integer cents up to `10**12`; UI parses positive finite decimal input with at most two fractional digits into cents. |
| 404 source/destination | Missing/foreign sources and system endpoints are deliberately unavailable; list the caller's accounts rather than guessing IDs. |
| 422 recipient not found | UI requires exactly one non-system `Checking` account for the normalized email. Unknown, missing, and ambiguous matches share this error. Numeric API destination fields do not substitute for `to_email`. |
| 422 self/currency/insufficient funds | UI rejects the sender's own email; ledger rejects differing currencies, equal endpoints, and overdrafts. Preserve these checks. |
| 422 key already used | The same user reused an idempotency key with a different source, destination, or amount. For a genuinely new transfer, use a new form/key. |
| 201 API replay or “already processed” UI result | Expected successful retry: same transfer, no second movement. Replay precedes the balance check but follows account validation. |
| Generic 503 during UI recipient lookup/transfer | The protected lookup/transfer block handles pool/query availability failures. This is not a universal error handler for all endpoints or authentication dependencies. |
| UI result has not changed | Wait for the htmx result swap; success also replaces the hidden key out of band. Plain POST returns the full page. |
| History seems incomplete | UI selects one owned account and up to 200 entries. API defaults to 100, permits 1–500, and returns newest entry IDs first. |

Sources: `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py`, `app/templates/_transfer_result.html`, `tests/test_transfer_errors.py`.

After an uncertain network outcome, retry the same payload/key rather than blindly creating a fresh transfer. For diagnosis compare balance deltas and history; initial seed balances are not reset by ordinary re-seeding. `/reconcile` only reports the global ledger sum—it does not repair data or verify every business rule.

For QA server mismatches, follow [Testing](testing.md): a changed checkout, wrong process, or wrong nonce means the server must not be reused. In CI report BLOCKED rather than launching another server. For available logs and their limits, see [Monitoring](../how-to-monitor.md).
