# Features

Active contributors: ptfeya

## Purpose

LedgerLite exposes the same ledger through a bearer-authenticated JSON API and cookie-authenticated HTML pages. Both route modules delegate transfers and account reads to `app/ledger.py`; the browser interface is not a separate money-movement implementation. Sources: `app/routes/api.py`, `app/routes/pages.py`, `app/auth.py`.

## Feature map

| Area | Behavior | Implementation |
| --- | --- | --- |
| [Transfers](transfers.md) | Integer-cent transfers, source ownership, ordered account locks, overdraft prevention and user-scoped idempotency | `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py` |
| [Accounts and history](accounts-and-history.md) | Owner-filtered account balances derived from entries, newest-first history and admin-only global reconciliation | `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py` |
| [Web interface](web-interface.md) | Login, logout, account list, email-recipient transfers and account history; ordinary forms plus htmx transfer results | `app/routes/pages.py`, `app/templates/base.html`, `app/templates/transfer.html` |

## Integration and modification

The API uses numeric destination account IDs. The browser resolves a recipient email to exactly one non-system account named `Checking` before calling the same transfer function. Browser self-payments are rejected; API transfers between different accounts belonging to the caller are allowed. Sources: `app/routes/api.py`, `app/ledger.py`, `tests/test_recipient_lookup.py`, `tests/test_overdraft.py`.

Keep money movement in `app/ledger.py`, amounts in integer cents, and CSRF checks on every form POST. Add regression tests when changing behavior. See [patterns and conventions](../how-to-contribute/patterns-and-conventions.md), [JSON API](../api.md), and [architecture](../overview/architecture.md). Sources: `AGENTS.md`, `app/routes/pages.py`.

## Scope and maintenance

For this navigation page, the implementation scope is exactly three Python files: `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py`. The selected regression scope is eight modules: `tests/test_idempotency.py`, `tests/test_overdraft.py`, `tests/test_ledger_zero_sum.py`, `tests/test_access_control.py`, `tests/test_system_account.py`, `tests/test_pages_and_csrf.py`, `tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`. Test-to-code file ratio: **8/3 = 2.67**. This is a file-count ratio, not coverage; fixtures, templates, schema and authentication implementation are outside this scope.

Git history on `origin/main` for those three implementation paths contains **one unique human author**, excluding names containing `[bot]`. This is a history-based maintenance signal, not an ownership guarantee.

## Key source files

| Path | Role |
| --- | --- |
| `app/ledger.py` | Shared transfer and read operations |
| `app/routes/api.py` | JSON endpoint contracts and error translation |
| `app/routes/pages.py` | HTML routes, form validation and response selection |
| `tests/test_access_control.py` | Read authorization and bearer-only API checks |
| `tests/test_pages_and_csrf.py` | Browser flows and CSRF regressions |
