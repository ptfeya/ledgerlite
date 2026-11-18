# Security

Active contributors: ptfeya.

LedgerLite separates bearer-authenticated JSON requests from cookie-authenticated HTML forms. This page describes current implementation boundaries and known non-features; it is not a security audit or a deployment guarantee.

## Identity and passwords

`app/security.py` hashes and verifies passwords with `argon2-cffi`'s default `PasswordHasher`. Invalid hashes and verification failures return false. Login trims and lowercases email before its parameterized database lookup in `app/auth.py`.

JWTs are signed with HS256 using `JWT_SECRET`. Issued claims are `sub` (string user ID), `iat`, and `exp`. Decoding allows HS256 only, requires `exp` and `sub`, validates expiry, and converts the subject to an integer. There is no issuer or audience configuration. Default lifetime is 3,600 seconds.

`JWT_SECRET` has no default and must contain at least 16 characters. The length check does not establish randomness or reject a recognizable placeholder. Use an independently generated secret; never commit `.env`. See [Configuration](reference/configuration.md).

Every authenticated request fetches the current user row, including `is_admin`. Removing a user makes their token unusable; changing their admin flag changes access on the next request. Tokens do not embed authoritative roles.

## Authentication transports and CSRF

| Surface | Identity input | State-changing request protection |
| --- | --- | --- |
| JSON API | `Authorization: Bearer <token>` only | Browser session cookies are ignored |
| HTML `/ui` pages | `ll_session` cookie only | Every form POST checks a CSRF token |
| HTML login | No authenticated session yet | Signed anonymous double-submit token |

`get_api_user` returns `401` with a Bearer challenge for invalid or missing credentials. `get_api_admin` additionally requires the current database admin flag and returns `403` otherwise. A valid admin cookie cannot elevate a regular bearer token. Missing/invalid HTML sessions redirect to `/ui/login` with `303`.

For login, `/ui/login` creates or reuses a signed random token in `ll_csrf`; the submitted form token must match that cookie and have a valid HMAC signature. Successful login deletes the anonymous cookie. Authenticated forms use an HMAC-SHA256 token bound to the exact session JWT. Tokens from a different session do not match. Comparisons use `hmac.compare_digest`.

Both cookies are set `HttpOnly`, `SameSite=Strict`, and `Path=/`. They are `Secure` unless the request hostname is exactly `localhost`, `127.0.0.1`, or `::1`. The anonymous cookie lasts 3,600 seconds; the session cookie uses the configured JWT TTL. This hostname exception is application behavior, not an automatic TLS setup.

Logout is a CSRF-protected POST that deletes `ll_session`. It does **not** revoke the JWT. A copied token can remain usable until expiry while its user still exists. There is no token denylist, refresh-token endpoint, password-reset flow, account-registration endpoint, MFA, or application login rate limiter.

Sources: `app/auth.py`, `app/security.py`, `app/routes/pages.py`, and `app/routes/api.py`. See [Web interface](features/web-interface.md) for the complete form flow.

## Authorization and money movement

`app/ledger.py` requires the caller to own the source account and rejects system accounts as either source or destination. The JSON API maps a missing, foreign, or system source to the same `404` error; UI business failures all map to generic `422` instead (`app/routes/api.py`, `app/routes/pages.py`). Entries for a foreign account are indistinguishable from a missing account. Destinations need not belong to the caller.

The system funding account has no user owner and no login. Its ownership rule is a database check constraint; blocking ordinary transfers to or from it is an application check. Opening funding in `app/seed.py` and local isolated fixtures in `tools/qa_harness.py` are explicit direct-ledger funding exceptions. Harness adjustments remain balanced and use ordered account locks; they do not authorize system-account movements through product endpoints.

Transfers use integer cents, require different same-currency accounts, and cap amounts at `10^12` cents. Account rows are locked in increasing ID order. A source balance check and balanced debit/credit insertion occur in the same transaction. Per-user idempotency prevents an accepted replay from moving funds twice. These application guarantees assume money movement uses the shared ledger function.

Only `POST /reconcile` requires admin status. It reads the global entry sum without repairing anything. Normal account and transfer permissions are not bypassed for admins.

## Recipient privacy and attempt limits

After local form validation, `app/routes/pages.py` maps all caught `TransferError` failures to `422` and “We could not complete this transfer. Check the recipient and your balance.” Unknown recipients, overdrafts, self-payments, currency or ownership failures, and idempotency conflicts do not expose different business-error responses. Full-page and htmx regressions compare status, body and headers; they do not establish timing equivalence (`tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`). The JSON API keeps its numeric-account error contract.

`app/rate_limits.py` limits `POST /ui/transfer` to 20 authenticated, CSRF-valid attempts per user per 60-second window beginning with the first attempt. An atomic upsert commits separately from money movement, so invalid fields, business failures and replays count too. A Postgres row in `db/schema.sql` shares the window across sessions, workers and restarts. CSRF failures are rejected before counting. Blocked attempts return `429` plus `Retry-After` before recipient lookup or transfer execution and do not extend the window (`app/routes/pages.py`, `tests/test_transfer_rate_limits.py`).

This is a UI-specific control, not a login limiter, API transfer limiter, IP limit or global abuse-prevention system. Successful payments still confirm recipient existence. Keep that residual disclosure separate from the generic failure contract (`README.md`, `app/routes/api.py`, `app/routes/pages.py`).

## Database safeguards and their limits

`db/schema.sql` provides foreign keys, ownership/amount checks, and per-user key uniqueness. Two ledger triggers provide narrower guarantees than the whole transfer service:

* `ledger_entries_balanced` is a row-level, deferred, initially deferred constraint trigger after **INSERT**. At transaction completion it checks the sum of entries for each inserted entry's transfer. A nonzero sum raises a check violation.
* `ledger_entries_append_only` is a row-level trigger before **UPDATE or DELETE**, raising an exception for affected ledger rows.

The balance trigger does not require exactly two entries, require entries to use the transfer's stated accounts or amount, or require a transfer to have entries at all. It does not enforce ownership, currency compatibility, nonnegative account balances, the application amount cap, or system-endpoint exclusion. Further balanced entries can be inserted for an existing transfer.

The append-only trigger does not cover `TRUNCATE`, protect metadata in other tables, or prevent an adequately privileged database role from altering/disabling triggers. Neither trigger substitutes for restricted database privileges. The global reconciliation sum can be zero even when individual business relationships are wrong. See [Data model](reference/data-models.md).

## Errors, secrets, and exposed surfaces

Login returns the same `Invalid credentials` message for unknown users and incorrect passwords. This is response-message equivalence, not a claim of timing indistinguishability. Handled login connection/pool failures return generic `503` responses; `app/auth.py` logs only the exception class, not exception text or a traceback that could include credentials. The UI transfer handler also maps specified connection/pool failures during attempt counting, recipient lookup and execution to a generic `503` (`app/routes/pages.py`). These handlers are not a global database-error sanitization layer.

SQL values are passed as parameters in the application. JSON API documentation and schema routes are disabled in `app/main.py`: `/docs`, `/redoc`, and `/openapi.json` return `404`.

There is no application-configured CORS middleware, general rate-limiting middleware, or production TLS/reverse-proxy setup in `app/main.py`; the UI transfer limiter is an explicit route call to `app/rate_limits.py`. Local Postgres publishing is restricted to `127.0.0.1` by `docker-compose.yml`; this does not define a production network policy.

## Review and regression support

### Local harness boundaries

`tools/qa_harness.py` derives a separate `_harness` target, checks local hosts/addresses or Unix socket paths and inherited libpq settings, refuses connection services and nonempty agent/evidence credentials, and checks database/role comment markers before managed creation/reset/cleanup. Saved state is bound to the configured database/server. These are local operational guards, not an audit or protection against malicious local code.

Its generated application role has restricted role attributes but owns the isolated database. That ownership is not a complete sandbox and does not strengthen the ledger triggers beyond their limits above. The scrubbed child environment reduces inherited credentials; `app/config.py` can still load working-directory `.env` fallback settings.

`qa-results/.qa-harness.json` is mode `0600` but contains actual database credentials and a JWT signing key: never publish or upload it. Inspect/redact the private server log before sharing. Public JSON includes a local base URL and the literal `SEED_PASSWORD` reference. Process/header nonces identify a saved run, not trusted code or authenticated evidence; the harness JSON error handler handles specified errors, not every possible exception or disclosure. See [QA harness](features/qa-harness.md) and [Configuration](reference/configuration.md). Sources: `tools/qa_harness.py`, `tests/test_qa_harness.py`.

`.factory/droids/security-reviewer.md` configures a read-only reviewer with `Read`, `Grep`, `Glob`, and `LS`. It does not enforce runtime policy or automatically establish that a change is safe. QA uses a throwaway database and separate application credentials; its server nonce/fingerprint verification checks freshness, not application trustworthiness.

Relevant regression sources: `tests/test_access_control.py`, `tests/test_pages_and_csrf.py`, `tests/test_system_account.py`, `tests/test_login_errors.py`, `tests/test_ledger_zero_sum.py`, `tests/test_api_docs_disabled.py`, `tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`, and `tests/test_transfer_rate_limits.py`. No runtime tests or audit probes were run for this documentation.

### Browser QA supply chain

`.github/workflows/qa.yml` prepares the browser stack in trusted steps without agent secrets, using the base branch's `.factory/skills/qa/scripts/qa-tools.sh`. The npm lockfile supplies integrity hashes and installation disables lifecycle scripts; Chrome's archive is SHA-256 checked; `.factory/settings.json` pins the droid-control marketplace to a full commit, and setup disables its automatic updates. These pins make the selected inputs reproducible; they do not establish that those inputs are safe.

The workflow attempts to re-enable unprivileged user namespaces on Ubuntu 24.04 for Chrome's sandbox rather than passing `--no-sandbox`. Its Droid CLI installer remains unpinned and unchecksummed. Browser preflight proves static-page rendering before probing the application, but does not attest to application trust or repair the remaining installer risk (`.github/workflows/qa.yml`, `.factory/skills/qa/scripts/qa-tools.sh`).

The keyed agent only reads preflight results and talks to the prestarted app. It must report tooling failures as BLOCKED and application failures as FAIL, not execute installation or fall back to curl for browser coverage (`.factory/skills/qa/SKILL.md`, `.factory/skills/qa-web/SKILL.md`). The reporting regressions in `tests/test_qa_tools.py` use stubs, not a real Chrome or plugin installation.

Related: [JSON API](api.md), [Transfers](features/transfers.md), and [Tooling](how-to-contribute/tooling.md).
