# Transfers

Active contributors: ptfeya

## Purpose

`execute_transfer` moves a positive integer-cent amount between two different non-system accounts, writing one debit and one equal credit in a single transaction. The caller must own the source, and currencies must match. The maximum is `10**12` cents. Sources: `app/ledger.py`, `app/routes/api.py`.

The [JSON API](../api.md) accepts account IDs at `POST /transfers`; the [web interface](web-interface.md) first resolves an email recipient. Neither route writes ledger entries directly. Sources: `app/routes/api.py`, `app/routes/pages.py`.

## Transaction flow

The order below follows `execute_transfer` in `app/ledger.py`:

1. Reject amounts outside `1..10**12` and identical source/destination IDs before acquiring a connection.
2. Hash the string `from_account_id|to_account_id|amount_cents` with SHA-256.
3. Enter `pool.connection()` and select both account rows with `ORDER BY id FOR UPDATE`. Locking in increasing account-ID order prevents opposite-direction transfers through this function from acquiring these locks in conflicting order.
4. Reject a missing, system or foreign-owned source with `AccountNotFound`; reject a missing or system destination similarly. Reject currency mismatch. Destination ownership need not match the caller.
5. Insert the transfer using `ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING id, created_at`.
6. On conflict, read the stored transfer. A different request hash raises `IdempotencyConflict`; a matching hash returns its original ID, fields and creation time with `replayed=True`.
7. Only for a newly inserted transfer, sum source entries under the account lock. Reject insufficient funds; spending exactly the balance is allowed.
8. Insert the negative source amount and positive destination amount in one SQL statement. Successful connection-context exit commits before the new result is returned.

The source lock serializes competing spends, so subsequent calls see the balance after an earlier committed spend. Both endpoints are locked because transfers can run in either direction. Regression cases exercise eight concurrent overspending attempts and opposite-direction transfers. Sources: `app/ledger.py`, `tests/test_overdraft.py`, `tests/test_ledger_zero_sum.py`.

## Replay and rollback

Keys are unique per user through the `(user_id, idempotency_key)` index. Different users may reuse the same text. API keys must contain 1 to 255 characters; the HTML route strips its submitted key and enforces that length. Sources: `db/schema.sql`, `app/routes/api.py`, `app/routes/pages.py`, `tests/test_idempotency.py`.

A matching replay bypasses the **balance check and new entry writes**, not all validation. Amount, distinct-account, account existence, source ownership, system-account and currency checks still run before replay detection. The UI also repeats recipient resolution first. A previously successful transfer can replay after its source has been drained, but changing account state or recipient resolution can prevent reaching replay detection. Sources: `app/ledger.py`, `app/routes/pages.py`, `tests/test_idempotency.py`, `tests/test_pages_and_csrf.py`.

An exception inside the transaction rolls back the inserted transfer and any entries. In particular, insufficient funds does not consume the key: a later request using that key can succeed with a smaller amount because no transfer was committed. A rejected conflicting payload leaves the original transfer intact. Sources: `app/ledger.py`, `tests/test_overdraft.py`, `tests/test_idempotency.py`.

The API returns `201` for both new transfers and replays, adding `Idempotent-Replay: true` only for a replay. Account errors map to `404`; other `TransferError` subclasses map to `422`. HTML success is `200`, with an “already processed” note for replays. Sources: `app/routes/api.py`, `app/routes/pages.py`, `app/ledger.py`.

## Database guarantees and limits

`db/schema.sql` adds positive transfer amounts, distinct endpoint IDs, foreign keys, nonzero entry amounts and the per-user key index. Its deferred, initially deferred constraint trigger checks each inserted entry's transfer sum at commit. A separate trigger rejects entry updates and deletes. Sources: `db/schema.sql`, `tests/test_ledger_zero_sum.py`.

The database trigger does **not** require exactly two entries, enforce correspondence between entry accounts and transfer endpoints, or require a transfer to have entries when none were inserted. Exactly one debit and one equal credit, source authorization, currency matching and sufficient funds are application guarantees of `app/ledger.py`, not stronger claims about the SQL trigger. See the [data model](../reference/data-models.md). Sources: `db/schema.sql`, `app/ledger.py`.

## Modification and subsystem scope

Preserve ordered locking, replay-before-balance ordering and one transaction around the transfer and entries. Extend concurrency, idempotency and rollback regressions with behavior changes; see [testing](../how-to-contribute/testing.md). Sources: `AGENTS.md`, `app/ledger.py`, `tests/test_idempotency.py`, `tests/test_overdraft.py`, `tests/test_ledger_zero_sum.py`.

Exact implementation scope: **2 files**, `app/ledger.py` and `db/schema.sql`. Exact selected test scope: **3 modules**, `tests/test_idempotency.py`, `tests/test_overdraft.py`, `tests/test_ledger_zero_sum.py`. Test-to-code file ratio: **3/2 = 1.50**, including SQL as implementation and excluding shared fixtures and route adapters. This is not coverage. These two implementation paths have **one unique human author** in `origin/main` history after filtering names containing `[bot]`.

## Key source files

| Path | Role |
| --- | --- |
| `app/ledger.py` | Locking, validation, replay, balance check and paired writes |
| `db/schema.sql` | Key uniqueness, constraints and ledger triggers |
| `app/routes/api.py` | API validation, status codes and replay header |
| `tests/test_idempotency.py` | Replays, conflicts, key scope and concurrent duplicate requests |
| `tests/test_overdraft.py` | Full-balance spending, rollback and concurrent spends |
| `tests/test_ledger_zero_sum.py` | Opposite-direction concurrency and database invariants |
