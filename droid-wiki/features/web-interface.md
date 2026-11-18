# Web interface

Active contributors: ptfeya

## Purpose

The browser interface renders Jinja templates under `/ui`, using a session cookie and CSRF-protected POST forms. Transfers work as ordinary form submissions or htmx requests to the same route. Sources: `app/routes/pages.py`, `app/templates/transfer.html`.

## Layout and routes

`app/main.py` registers the page routers and serves `/static`. `app/templates/base.html` supplies account/transfer/history navigation, logout, stylesheet and vendored htmx script. Missing or invalid sessions on protected pages become `303` redirects to `/ui/login`. Sources: `app/main.py`, `app/auth.py`, `app/templates/base.html`.

All page routes are defined in `app/routes/pages.py`:

| Method and path | Behavior |
| --- | --- |
| `GET /` | `303` to `/ui/accounts` |
| `GET /ui/login` | Login template; reuse a valid signed anonymous CSRF cookie or create one with a one-hour cookie lifetime |
| `POST /ui/login` | Validate signed double-submit CSRF; empty credentials `422`, invalid credentials `401`, authentication database outage `503`; success sets session, deletes anonymous CSRF cookie and redirects `303` to accounts |
| `POST /ui/logout` | Require session-bound CSRF, delete session cookie, redirect `303` to login |
| `GET /ui/accounts` | Render caller's derived account balances |
| `GET /ui/transfer` | Render caller's accounts and a fresh UUID idempotency key |
| `POST /ui/transfer` | Validate CSRF, count/limit attempts, validate inputs, resolve recipient, execute transfer, return fragment or complete page |
| `GET /ui/history` | Select optional integer `account_id`, otherwise first owned account; read up to 200 entries, or render empty history when there are no accounts; foreign/missing selection is `404` |

`app/templates/login.html`, `app/templates/accounts.html`, `app/templates/transfer.html` and `app/templates/history.html` are the full-page templates. `app/templates/_transfer_result.html` supplies the result fragment. The history selector auto-submits with JavaScript and includes a no-script submit button. Sources: those template paths and `app/routes/pages.py`.

## Form validation and recipient resolution

The transfer form submits `from_account_id`, `to_email`, `amount`, `idempotency_key` and `csrf_token`. It does not accept a numeric destination account as an alternative to email. Source IDs must be stripped ASCII digits shorter than 18 characters; source ownership is subsequently checked by the ledger. Sources: `app/templates/transfer.html`, `app/routes/pages.py`, `app/ledger.py`, `tests/test_recipient_lookup.py`.

Amount parsing uses `Decimal` on stripped text, rejects nonfinite/nonpositive values or an exponent below `-2`, converts to integer cents and enforces `10**12` cents maximum. The precision rule concerns the Decimal representation: `1.230` is rejected rather than rounded. The form labels amounts USD, while the ledger checks that source and destination currencies match. Money display formats integer cents with a dollar sign, comma separators and two decimal places. Sources: `app/routes/pages.py`, `app/templates/transfer.html`, `app/ledger.py`.

`resolve_recipient_account` strips and lowercases email, checks its regex syntax, then joins users/accounts for exactly one non-system account whose name is exactly `Checking`. The query fetches at most two matches to detect ambiguity. Invalid syntax, unknown user, no Checking account and duplicate Checking accounts raise the same internal recipient error; a resolved recipient belonging to the caller raises a self-transfer error. The HTML route hides both distinctions, along with all other caught ledger business errors, behind generic `422`: “We could not complete this transfer. Check the recipient and your balance.” Source-ID, amount and key form-validation messages remain specific. The JSON API retains its numeric-account behavior and permits transfers between a user's different accounts. Sources: `app/ledger.py`, `app/routes/pages.py`, `tests/test_recipient_lookup.py`, `tests/test_pages_and_csrf.py`, `tests/test_transfer_errors.py`, `tests/test_overdraft.py`.

## Transfer attempt limit

After authentication and CSRF validation, `transfer_submit` in `app/routes/pages.py` calls `transfer_retry_after` in `app/rate_limits.py`. Each user gets 20 attempts in a 60-second window beginning with their first attempt. Invalid fields, business failures, successes and replays all count. CSRF failures do not. The limiter runs before recipient lookup or transfer execution, so blocked requests cannot probe recipients through those operations (`tests/test_recipient_lookup.py`, `tests/test_transfer_rate_limits.py`).

An atomic upsert in a separate committed connection context updates one `transfer_rate_limits` row per user. Its counter saturates at 21. Sessions, workers and app restarts share that row; failed money movement cannot roll it back. Blocked attempts do not extend the window; the next attempt after expiry resets it to one (`app/rate_limits.py`, `db/schema.sql`).

Over-limit requests render `429` with `Retry-After` in whole seconds and “Too many transfer attempts. Please try again shortly.” Both full pages and htmx fragments preserve the stripped submitted key. The limiter applies only to this UI POST, not JSON transfers or login. Success still confirms a recipient exists; the limit reduces probing rather than making money movement undiscoverable (`app/routes/pages.py`, `app/routes/api.py`, `tests/test_transfer_rate_limits.py`). Existing databases need schema reapplication through `app/seed.py`; restarting the app does not create the table.

## htmx, plain POST and retries

`app/templates/transfer.html` has both normal `method="post" action="/ui/transfer"` and `hx-post`, targeting `#result` with `innerHTML`. A truthy `HX-Request` header selects `app/templates/_transfer_result.html`; without it, the route renders the complete transfer page and reloads the account list. Plain success is a rendered `200`, not a redirect. Sources: `app/templates/transfer.html`, `app/routes/pages.py`.

Both new and replayed successes rotate the form key to a new UUID. htmx receives a hidden input with `hx-swap-oob="true"` so the next transfer uses the refreshed key. Failure keeps the submitted, stripped key. Resubmitting an old successful key with the same resolved account IDs and amount replays once, with an “already processed” note; a changed payload conflicts. Recipient resolution still runs before replay lookup. Sources: `app/routes/pages.py`, `app/templates/_transfer_result.html`, `app/ledger.py`, `tests/test_pages_and_csrf.py`.

The base template configures htmx to swap error responses too. All caught business errors use the same generic `422` HTML response, regardless of their internal ledger status. `OperationalError`, `InterfaceError` or `PoolTimeout` during attempt counting, recipient lookup or execution instead produces generic JSON `503 Service temporarily unavailable`, not a result fragment. These catches do not surround every page/authentication/account-list query. Sources: `app/templates/base.html`, `app/routes/pages.py`, `tests/test_transfer_errors.py`, `tests/test_recipient_lookup.py`.

Only the result and key refresh on htmx success; the account-balance table is not replaced. A full form submission rerenders that table, and accounts/history can be revisited for updated reads. Sources: `app/templates/transfer.html`, `app/templates/_transfer_result.html`, `app/routes/pages.py`.

## Session integration and modification

Login uses a signed anonymous cookie/form token pair. Authenticated forms use an HMAC token bound to the session JWT. Cookies are HttpOnly and SameSite Strict, with Secure omitted only for `localhost`, `127.0.0.1` and `::1`. Logout deletes the browser cookie but does not revoke the JWT. Sources: `app/security.py`, `app/routes/pages.py`.

Keep full-page and fragment behavior aligned when changing forms, preserve CSRF on login/logout/transfer, and leave money movement in the shared ledger. See [security](../security.md), [transfers](transfers.md) and [accounts and history](accounts-and-history.md). Sources: `AGENTS.md`, `app/routes/pages.py`, `tests/test_pages_and_csrf.py`.

## Scope and maintenance

Exact UI implementation scope: **10 files**, `app/routes/pages.py` and `app/rate_limits.py`; six templates (`app/templates/base.html`, `app/templates/login.html`, `app/templates/accounts.html`, `app/templates/transfer.html`, `app/templates/_transfer_result.html`, `app/templates/history.html`); and two assets (`app/static/style.css`, `app/static/htmx.min.js`). Exact selected test scope: **4 modules**, `tests/test_pages_and_csrf.py`, `tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`, `tests/test_transfer_rate_limits.py`. Test-to-code file ratio: **4/10 = 0.40**, counting templates and vendored assets, excluding shared auth/ledger modules, schema and fixtures. This is not coverage. These ten implementation paths have **one unique human author** in `origin/main` history after filtering names containing `[bot]`.

## Key source files

| Path | Role |
| --- | --- |
| `app/routes/pages.py` | All UI routes, parsers, CSRF enforcement and response branching |
| `app/rate_limits.py` | Atomic committed attempt counter and retry delay |
| `db/schema.sql` | Persistent per-user rate-limit table |
| `app/ledger.py` | Recipient resolution and transfer execution |
| `app/templates/base.html` | Navigation, logout and htmx response handling |
| `app/templates/transfer.html` | Plain/htmx form and source-balance table |
| `app/templates/_transfer_result.html` | Feedback and out-of-band key replacement |
| `tests/test_pages_and_csrf.py` | Complete flows, replay and CSRF |
| `tests/test_recipient_lookup.py` | Resolution edge cases and isolated route delegation |
| `tests/test_transfer_errors.py` | Retryable, non-disclosing database failures |
| `tests/test_transfer_rate_limits.py` | Atomic limits, expiry, sessions, replays, and no writes while blocked |
