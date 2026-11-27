# Web interface

Active contributors: ptfeya

## Purpose

The browser interface renders Jinja templates under `/ui`, using a session cookie and CSRF-protected POST forms. Transfers work as ordinary form submissions or htmx requests to the same route. Sources: `app/routes/pages.py`, `app/templates/transfer.html`.

## Layout and routes

`app/main.py` registers the page routers and serves `/static`. `app/templates/base.html` supplies the branded header, the Accounts/Transfer/History tab bar, session greeting, logout, footer, stylesheet and vendored htmx script. Missing or invalid sessions on protected pages become `303` redirects to `/ui/login`. Sources: `app/main.py`, `app/auth.py`, `app/templates/base.html`.

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

## Branding and styling

Since [commit 06b1100](https://github.com/ptfeya/ledgerlite/commit/06b11005cba3c4cd0d69936230f7ffe0e6477087) (2026-11-27) the UI is styled as **Peachtree Federal Bank**, a fictional bank. [Commit 6077bfb](https://github.com/ptfeya/ledgerlite/commit/6077bfba99236627f77a1f343ae92ba6b1d0a096) (2026-11-27, #30) added an account summary on the history page, a session block in the header and a two-line footer. [Commit f911a85](https://github.com/ptfeya/ledgerlite/commit/f911a8585ecc8e1c9c2c582d20a38b84337654ee) (2026-11-27, #31) moved navigation into a tab bar, replaced the account card grid with a "Bank accounts" panel and a security card, and added monthly deposit and withdrawal totals to the history summary. All three changes touched only templates and CSS; routes, forms, CSRF and ledger behavior did not change. Sources: `app/templates/base.html`, `app/templates/accounts.html`, `app/templates/history.html`, `app/static/style.css`.

- **Header.** `app/templates/base.html` renders an inline SVG peach mark, the name "Peachtree Federal Bank" and the subline "Payments · LedgerLite sandbox" on every page, including the login page. For signed-in users only, a `.session` block shows "Welcome back, *Name*" and "Secure session · *email*" with a padlock SVG, followed by the logout form. *Name* is derived in the template from the email's local part (dots and underscores become spaces, then title case), so `alice@example.com` shows "Welcome back, Alice". There is no display-name field. Jinja autoescaping applies to both values.
- **Tab bar.** Below the header, signed-in users get a `nav.tabs` (`aria-label="Main"`) with Accounts, Transfer and History links. The template compares `request.url.path` with each link's path and marks the exact match with `class="active"` and `aria-current="page"`, drawn as a peach underline. `request` is available because `render` in `app/routes/pages.py` passes it to `TemplateResponse`. The comparison ignores the query string, so `/ui/history?account_id=2` still highlights History, and the rendered result of a full `POST /ui/transfer` highlights Transfer. The login page shows no tabs.
- **Footer.** Every page ends with two lines: "Peachtree Federal Bank · Member FDIC · Equal Housing Lender" and "Demo environment · fictional data". The first line is decorative text for the fictional brand.
- **Titles.** The base template's default `<title>` is "Peachtree Federal Bank", but each full-page template still overrides it with "… · LedgerLite" (for example "Accounts · LedgerLite"), so browser tabs keep the LedgerLite name.
- **Accounts page.** `app/templates/accounts.html` uses a two-column `.layout` grid. The main column is a collapsible `<details class="panel" open>` titled "Bank accounts", with a chevron that rotates when the panel is closed. Each account is an `.account-row` with a link "Peachtree *Name* (…*NNNN*) ›" to that account's history, the balance as a large `.figure`, and a dotted "Available balance" label whose tooltip says the demo ledger has no holds. Two buttons follow: "Transfer money" (`/ui/transfer`, with no account preselected) and "Statements" (the same history URL as the account link). *NNNN* is the database account ID modulo 10,000, zero-padded (`'%04d' | format(account.id % 10000)`); it is not a real account number. With no accounts, the panel shows "No accounts to show yet." The side column is an `aside.panel.security` card with a padlock, "Secure session active" and "Signed in as *email*".
- **History summary.** `app/templates/history.html` keeps the account selector in a `.card.toolbar`. When the selected account is in the user's account list, a `section.panel.summary` follows with the pill, title, masked number "•••• •••• *NNNN*", and current and available balance, each with a dotted tooltip label. If the account has entries, a side column adds "Deposits this month" (green) and "Withdrawals this month" (red) with a month label, then a "Recent activity" bar chart of up to 14 of the newest entries, drawn oldest to newest. Bars are green for credits and red for debits, with heights scaled to the largest absolute amount and a 10% minimum.
- **History table.** The `table.ledger` columns are Date (`%b %d, %Y` with the time below), Description ("Transfer received"/"Transfer sent" plus "Credit/Debit · Ref" and the first eight characters of the transfer UUID), Status, Amount and Balance. The empty state reads "No entries yet. Transfers you send or receive will appear here."
- **Stylesheet.** `app/static/style.css` defines the palette as CSS custom properties on `:root` (`--navy` `#0F2A44`, `--peach` `#F4A261`, plus background, line, text, muted, green and red), a peach `:focus-visible` outline, navy table headers with striped rows, and full-width inputs. The `.ok`, `.error`, `.credit` and `.debit` classes used by the result fragment and history keep their green/red meanings and are bold. `main` is 68rem wide with a 2rem top margin. `.card` (peach top border) still frames the login form, transfer form and history toolbar. f911a85 removed the header `nav` rules and the card-grid classes (`.cards`, `.card-top`, `.card-id`, `.card-name`, `.card-balance`, `.card-link`) and added `.tabs`, `.layout`, `.panel`, `.chevron`, `.account-row`, `.account-link`, `.figure`, `.dotted`, `.btn`/`.btn-solid`/`.btn-light`, `.security`, `.secure-line`, `.summary-side` and `.kv.month`. Below 800px the summary and the accounts layout collapse to one column and the session block aligns left.
- **Cache busting.** `base.html` links `/static/style.css?v=peachtree-3` (bumped from `peachtree-2` in f911a85). `StaticFiles` ignores the query string; the value only forces browsers to fetch the new file. Change the `v` value when a stylesheet change must reach browsers that cached the old one.

### Display-only values

Several values on these pages are computed in the templates or fixed in markup, not read from the ledger. Treat them as presentation until the ledger supports the concept:

| Shown value | Where it comes from |
| --- | --- |
| Available balance | The same `balance_cents` as the current balance; the ledger has no holds or available-funds concept |
| Deposits and withdrawals this month | Sums computed in `app/templates/history.html` over the loaded entries whose `created_at` falls in the same year and month as the newest entry. "This month" is that entry's month, not the current calendar month, and only the up-to-200 entries the route loaded are counted |
| Status | Every row gets a literal "Posted" badge; the `.badge.pending` style exists but nothing uses it |
| Balance column | A running balance computed in `app/templates/history.html`: it starts at the account's current balance and subtracts each entry while walking the newest-first list, so each row shows the balance just after that entry |
| Masked account number | Account ID modulo 10,000, as above |

The running balance is correct for the 200 entries shown because they are the newest ones, ordered by entry ID descending (see [accounts and history](accounts-and-history.md)). `history_page` in `app/routes/pages.py` reads the balances with `list_accounts` and the entries with `list_entries`, each on its own pool connection. A transfer that commits between the two reads can make the Balance column disagree with the newest row on that one render; reloading fixes it. The monthly totals have the same 200-entry limit: an account with more than 200 entries in its newest month shows partial totals.

No test asserts markup, titles or styles. The page tests in `tests/test_pages_and_csrf.py` check status codes, CSRF, form behavior and body text such as `$1,000.00` and `Savings` on the account page and `-$12.34` on history, which the new panel and table still render. The visual side of template-only changes like these is covered only by manual or browser QA (see [Testing](../how-to-contribute/testing.md)).

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

Only the result and key refresh on htmx success; the account-balance table inside the transfer card is not replaced. A full form submission rerenders that table, and accounts/history can be revisited for updated reads. Sources: `app/templates/transfer.html`, `app/templates/_transfer_result.html`, `app/routes/pages.py`.

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
| `app/templates/base.html` | Brand header, session block, logout, tab bar with active-page marking, footer and htmx response handling |
| `app/static/style.css` | Peachtree palette variables, tabs, panels, account rows, buttons, summary, sparkline, badges, tables, forms and status colors |
| `app/templates/accounts.html` | "Bank accounts" panel with account rows, action buttons and security card |
| `app/templates/history.html` | Account selector, summary panel, monthly totals, activity bars and running-balance table |
| `app/templates/transfer.html` | Plain/htmx form and source-balance table |
| `app/templates/_transfer_result.html` | Feedback and out-of-band key replacement |
| `tests/test_pages_and_csrf.py` | Complete flows, replay and CSRF |
| `tests/test_recipient_lookup.py` | Resolution edge cases and isolated route delegation |
| `tests/test_transfer_errors.py` | Retryable, non-disclosing database failures |
| `tests/test_transfer_rate_limits.py` | Atomic limits, expiry, sessions, replays, and no writes while blocked |
