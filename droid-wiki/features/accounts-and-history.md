# Accounts and history

Active contributors: ptfeya

## Purpose

Accounts expose balances derived from ledger entries and owner-authorized entry history. There is no stored balance column to update alongside a transfer. Sources: `app/ledger.py`, `db/schema.sql`.

## Read abstractions and authorization

`list_accounts(pool, user_id)` left-joins accounts to entries, filters `a.user_id = user_id`, groups by account and orders by ascending account ID. Its `COALESCE(SUM(...), 0)::bigint` returns integer-cent balances, including zero for an account without entries. Returned fields are `id`, `name`, `currency`, `balance_cents`. Sources: `app/ledger.py`, `app/routes/api.py`.

`list_entries` first checks the account ID and owner together. A missing or foreign-owned account returns `None`, which both route layers translate to `404 Account not found`; an owned account without entries returns an empty list. Entries include ID, transfer ID, account ID, signed amount and creation time, ordered by **entry ID descending**, not timestamp. Sources: `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py`, `tests/test_access_control.py`.

| Surface | Account list and history |
| --- | --- |
| JSON | `GET /accounts`; `GET /accounts/{account_id}/entries?limit=100`, with accepted limits `1..500` |
| HTML | `GET /ui/accounts`; `GET /ui/history?account_id=...`, reading at most 200 entries |

API authentication accepts bearer tokens only; HTML uses the session cookie. Both obtain a current database-backed user before passing its ID to ledger reads. Sources: `app/auth.py`, `app/routes/api.py`, `app/routes/pages.py`.

Without an explicit history selection, HTML chooses the first account in the ordered account list. With no accounts, it renders empty history. Its template shows time, transfer UUID, credit/debit classification and signed formatted amount. The account page links directly to each account's history. Sources: `app/routes/pages.py`, `app/templates/history.html`, `app/templates/accounts.html`.

See [web interface](web-interface.md) for navigation and [JSON API](../api.md) for endpoint contracts.

## Funding and reconciliation boundary

`app/seed.py` creates one ownerless system funding account and four demo accounts: Alice's Checking/Savings receive 100,000/50,000 cents; Bob's receive 75,000/25,000 cents. Each opening balance has a matching negative system entry. Seeding returns without adding demo data if any user already exists. Sources: `app/seed.py`.

The schema requires system accounts to have no owner and ordinary accounts to have an owner. Owner-filtered reads therefore do not expose the system account. Normal transfers reject it as either endpoint, including forged HTML source IDs. Sources: `db/schema.sql`, `app/ledger.py`, `tests/test_system_account.py`.

`ledger_imbalance` sums **all** entries, including funding entries. `POST /reconcile` requires an API admin and returns `{"status":"ok"}` for zero or `{"status":"imbalance","imbalance_cents":...}` otherwise. It does not repair balances, check each transfer individually, or prove the absence of offsetting errors. Admin status is reread from the database on each authenticated request. Sources: `app/ledger.py`, `app/routes/api.py`, `app/auth.py`, `tests/test_access_control.py`, `tests/test_ledger_zero_sum.py`.

## Modification and subsystem scope

Keep authorization in shared reads rather than relying on the HTML account selector. Preserve entry-ID ordering and route-specific limits when changing history behavior. Balance changes should come from [transfers](transfers.md), not direct balance storage. Sources: `app/ledger.py`, `app/routes/api.py`, `app/routes/pages.py`, `AGENTS.md`.

Exact implementation scope: **4 files**, `app/ledger.py`, `app/seed.py`, `app/routes/api.py`, `app/routes/pages.py`. Exact selected test scope: **3 modules**, `tests/test_access_control.py`, `tests/test_system_account.py`, `tests/test_ledger_zero_sum.py`. Test-to-code file ratio: **3/4 = 0.75**. Counts include whole shared modules, exclude templates/schema/fixtures, and do not measure coverage. These four implementation paths have **one unique human author** in `origin/main` history after filtering names containing `[bot]`.

## Key source files

| Path | Role |
| --- | --- |
| `app/ledger.py` | Derived balances, ownership checks, ordered entries and global sum |
| `app/seed.py` | Balanced opening funding and demo account definitions |
| `app/routes/api.py` | API read limits and admin reconciliation |
| `app/routes/pages.py` | HTML account selection and history limit |
| `tests/test_access_control.py` | Ownership, current admin state and auth transport regressions |
| `tests/test_system_account.py` | System-account invisibility and endpoint restrictions |
