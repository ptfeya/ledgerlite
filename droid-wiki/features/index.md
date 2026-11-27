# Features

Active contributors: ptfeya

## Purpose

LedgerLite exposes the same ledger through a bearer-authenticated JSON API and cookie-authenticated HTML pages. Both route modules delegate transfers and account reads to `app/ledger.py`; the browser interface is not a separate money-movement implementation. The one exception is the JSON statement export, which queries entries in the route without an ownership check ([Accounts and history](accounts-and-history.md)). Sources: `app/routes/api.py`, `app/routes/pages.py`, `app/auth.py`.

## Feature map

| Area | Behavior | Implementation |
| --- | --- | --- |
| [Transfers](transfers.md) | Integer-cent transfers, source ownership, ordered account locks, overdraft prevention and user-scoped idempotency | `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py` |
| [Accounts and history](accounts-and-history.md) | Owner-filtered account balances derived from entries, newest-first history and admin-only global reconciliation | `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py` |
| [Web interface](web-interface.md) | Login, logout, accounts and history; email-recipient transfers with generic business errors, per-user attempt limits, and plain/htmx results | `app/routes/pages.py`, `app/rate_limits.py`, `app/templates/base.html`, `app/templates/transfer.html` |

Local development tooling is documented separately in [QA harness](qa-harness.md): isolated fixtures, database outages and application restarts. It is outside the product-feature scope measured below. Sources: `tools/qa_harness.py`, `tools/README.md`.

Browser QA setup and its pinned-tool preflight are covered in [Tooling](../how-to-contribute/tooling.md). They are CI/developer tooling, not a new application feature (`.factory/skills/qa/scripts/qa-tools.sh`, `.github/workflows/qa.yml`).

## Integration and modification

The API uses numeric destination account IDs. The browser resolves a recipient email to exactly one non-system account named `Checking` before calling the same transfer function. Browser self-payments are rejected; API transfers between different accounts belonging to the caller are allowed. Sources: `app/routes/api.py`, `app/ledger.py`, `tests/test_recipient_lookup.py`, `tests/test_overdraft.py`.

Keep money movement in `app/ledger.py`, amounts in integer cents, and CSRF checks on every form POST. Add regression tests when changing behavior. See [patterns and conventions](../how-to-contribute/patterns-and-conventions.md), [JSON API](../api.md), and [architecture](../overview/architecture.md). Sources: `AGENTS.md`, `app/routes/pages.py`.

## Scope and maintenance

For this navigation page, the implementation scope is exactly four Python files: `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py`, `app/rate_limits.py`. The selected regression scope is nine modules: `tests/test_idempotency.py`, `tests/test_overdraft.py`, `tests/test_ledger_zero_sum.py`, `tests/test_access_control.py`, `tests/test_system_account.py`, `tests/test_pages_and_csrf.py`, `tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`, `tests/test_transfer_rate_limits.py`. Test-to-code file ratio: **9/4 = 2.25**. This is a file-count ratio, not coverage; fixtures, templates, schema and authentication implementation are outside this scope.

Git history on `origin/main` for those four implementation paths contains **one unique human author**, excluding names containing `[bot]`. This is a history-based maintenance signal, not an ownership guarantee.

## Key source files

| Path | Role |
| --- | --- |
| `app/ledger.py` | Shared transfer and read operations |
| `app/routes/api.py` | JSON endpoint contracts and error translation |
| `app/routes/pages.py` | HTML routes, form validation and response selection |
| `app/rate_limits.py` | Database-backed per-user UI transfer attempt limits |
| `tests/test_access_control.py` | Read authorization and bearer-only API checks |
| `tests/test_pages_and_csrf.py` | Browser flows and CSRF regressions |
