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
| API 404 source/destination | Missing/foreign sources and system endpoints are deliberately unavailable; list the caller's accounts rather than guessing IDs. The UI hides these distinctions behind generic 422. |
| UI generic 422 business failure | “We could not complete this transfer. Check the recipient and your balance.” covers unknown/malformed recipients, missing/ambiguous Checking accounts, self-payments, ownership, currency, balance and key conflicts. Numeric destination fields do not substitute for `to_email`. Do not expose internal errors to distinguish recipients. |
| API 422 currency/insufficient funds/key conflict | The API retains its detailed ledger errors. A conflicting key used a different source, destination or amount; retry uncertain outcomes with the original payload/key, and use a new key only for genuinely new work. |
| UI 429 with `Retry-After` | Twenty CSRF-valid attempts have used this user's 60-second fixed window. Failures and replays count; a new login or app restart does not reset the Postgres counter. Wait the header's delay before retrying the same payload/key. |
| 201 API replay or “already processed” UI result | Expected successful retry: same transfer, no second movement. Replay precedes the balance check but follows account validation. |
| Generic 503 during UI attempt counting/recipient lookup/transfer | The protected blocks handle pool/query availability failures. This is not a universal error handler for all endpoints or authentication dependencies. |
| Missing `transfer_rate_limits` relation after updating | Apply the new table to the intended development database with `uv run python -m app.seed`. Startup does not apply schema; seeding preserves existing data and counters. |
| UI result has not changed | Wait for the htmx result swap; success also replaces the hidden key out of band. Plain POST returns the full page. |
| History seems incomplete | UI selects one owned account and up to 200 entries. API defaults to 100, permits 1–500, and returns newest entry IDs first. |

Sources: `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py`, `app/rate_limits.py`, `db/schema.sql`, `app/seed.py`, `app/templates/_transfer_result.html`, `tests/test_transfer_errors.py`, `tests/test_transfer_rate_limits.py`.

After an uncertain network outcome, retry the same payload/key rather than blindly creating a fresh transfer. For diagnosis compare balance deltas and history; initial seed balances are not reset by ordinary re-seeding. `/reconcile` only reports the global ledger sum—it does not repair data or verify every business rule.

For QA server mismatches, follow [Testing](testing.md): a changed checkout, wrong process, or wrong nonce means the server must not be reused. In CI report BLOCKED rather than launching another server. For available logs and their limits, see [Monitoring](../how-to-monitor.md).

## Browser QA preflight runbook

In CI, read `qa-results/preflight.txt` before browser interactions. `.factory/skills/qa/scripts/qa-tools.sh` returns zero even for blockers or application findings, so inspect its contents rather than the step badge. Source contracts: `.factory/skills/qa/SKILL.md`, `.factory/skills/qa-web/SKILL.md`, `.github/workflows/qa.yml`.

| Symptom | Safe next action |
| --- | --- |
| File missing/unreadable | Report affected web flows BLOCKED: “preflight did not run”. Continue affected API flows; the keyed agent must not rerun setup. |
| `BLOCKED install`, `pins`, `agent-browser`, `chrome`, or `droid-control` | Quote the check/reason and add Action Required. A maintainer should inspect the trusted installation log/manifests and rerun CI; do not repair/download tools from the keyed agent. |
| `BLOCKED browser-launch` or `browser-render` | The independent static page failed, so report tooling BLOCKED, not an app failure. Check trusted browser setup/sandbox prerequisites outside the keyed run; do not disable the sandbox or substitute curl web flows. |
| `BLOCKED ffmpeg` or `imagemagick` | Config requested a media dependency that did not work. Repair approved preparation/configuration outside the keyed run. |
| `BLOCKED tooling: tooling not yet on base branch` | The workflow refused candidate-only tooling. Rerun after trusted tooling reaches the base; do not execute the candidate installer. |
| `BLOCKED qa-server` | URL/probe was unusable. Report the reason; never launch a replacement server from the keyed agent. |
| `APP_FAIL ui-login` even with `QA_PREFLIGHT: OK` | Static browser rendering passed but the app returned an unexpected HTTP status or lacked its password field. Report affected web flows FAIL, investigate app/server behavior, and continue flows the page permits. |
| Local browser checks fail | Run the prescribed `agent-browser --version` and `agent-browser doctor --offline --quick`; report BLOCKED. Obtain approval before dependency additions/downloads. |

All tooling blockers apply to every affected web flow, not API flows. See [Tooling](tooling.md) for exact pins and [Testing](testing.md) for report rules.

## Local harness runbook

Use this runbook only for the separate [QA harness](../features/qa-harness.md), not the shell `_qa` instance or persistent development data. Source: `tools/qa_harness.py`; command reference: `tools/README.md`.

| Symptom | Safe next action |
| --- | --- |
| Credential, locality, service or identifier refusal | Check environment-variable names and connection configuration without printing credentials. Run only in an approved local environment without agent/evidence credentials; do not bypass guards or execute checkout code from a keyed CI agent. |
| Saved state belongs to a different target | Restore the original `DATABASE_URL` server/database configuration, then run `uv run python -m tools.qa_harness down`. Do not rewrite/delete state to evade the target check. |
| Same-named database or role lacks its marker | Stop and investigate the resource's ownership. Do not add markers, force-drop it or bypass the refusal. |
| Login returns `503` after outage simulation | Inspect `uv run python -m tools.qa_harness outage status`, then restore with `uv run python -m tools.qa_harness outage off`. Restore connections before fixtures or restart. |
| Restart says the saved port is occupied | Check the known process using that port. Do not kill an unrelated service. Restart needs bound saved state, not necessarily a live old process; it deliberately keeps the saved port. |
| Startup exits or does not answer within 30 seconds | Inspect `qa-results/qa-harness-server.log` privately for startup/configuration failures and local Postgres readiness. Do not publish raw logs or read/paste the secret-bearing `qa-results/.qa-harness.json` as diagnostic evidence. |
| Status says `running` but requests fail | `running` checks a matching process nonce, not application health. Check outage state and local HTTP behavior separately; the nonce establishes process freshness, not trust. |

After the approved local investigation, run `uv run python -m tools.qa_harness down` against the original target to stop the matching app, drop its marked isolated database/role, and remove state/log files. Reset commands destroy only isolated fixture data and are not a repair for development data or an unmarked resource. No harness commands were executed for this documentation update.
