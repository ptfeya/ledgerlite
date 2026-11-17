# Patterns and conventions

The rules in `AGENTS.md` protect money movement and authentication boundaries. Match the existing typed Python, SQL parameterization, and route-to-ledger split when changing behavior (`app/ledger.py`, `app/auth.py`, `app/routes/api.py`, `app/routes/pages.py`).

## Money and transactions

- Store and pass integer cents. `Decimal` is only for parsing UI input in `app/routes/pages.py`; do not introduce floating-point money.
- Send ordinary transfers through `execute_transfer` in `app/ledger.py`. Routes must not insert ledger entries.
- Keep the debit, credit, and transfer row in one transaction. `pool.connection()` in `app/ledger.py` commits on success and rolls back on exceptions.
- Lock both accounts in ascending ID order before checking the source balance. Preserve that ordering for all cooperating transfer writers.
- Reject ownerless system funding accounts as transfer endpoints. Direct balanced funding writes in `app/seed.py` are a seed operation, not a pattern for routes.

The deferred balance trigger in `db/schema.sql` runs at commit, so a successful insert statement alone does not prove the transaction will commit. Preserve tests that inspect committed state and concurrent outcomes. See [Transfers](../features/transfers.md).

## Input and errors

`app/routes/api.py` uses Pydantic request/response models and bounded headers/query parameters. `app/routes/pages.py` parses form strings explicitly, then renders full HTML or htmx fragments. Both call the same ledger functions instead of copying business rules.

`TransferError` subclasses in `app/ledger.py` define expected transfer failures. Account-not-found errors map to 404; invalid input, insufficient funds, and idempotency conflicts map to 422. Do not substitute a different response code without updating callers and tests.

`authenticate` in `app/auth.py` normalizes email and handles database/pool failures with a generic application error. Its log includes only the exception class, not the exception message or traceback. Preserve indistinguishable responses for unknown credentials and ambiguous/missing recipients (`app/auth.py`, `app/ledger.py`).

## Authentication and forms

Keep `get_api_user` and `get_page_user` in `app/auth.py` separate: bearer tokens authenticate JSON; cookies authenticate HTML. `get_api_admin` adds the current database admin check. Never accept a session cookie as an API fallback.

Every form POST needs CSRF, including login and logout. Reuse `require_csrf`, `render`, and the anonymous-token helpers rather than inventing a separate token format (`app/routes/pages.py`, `app/security.py`). See [Security](../security.md).

`app/templates/base.html` enables htmx swapping for error responses. `app/templates/_transfer_result.html` can replace the hidden idempotency key out of band. Keep plain form submission working as well as htmx.

## Python and SQL

`pyproject.toml` targets Python 3.12 with a 100-column Ruff configuration. Selected rule families are E, F, I, B, UP, and SIM; FastAPI dependency/form defaults have an explicit immutable-call exception. `app/static/` is excluded from Ruff.

Use `%s` query parameters for values, as in `app/ledger.py`. Use identifier composition for dynamic identifiers, as in `tests/conftest.py`. Frozen dataclasses carry request user context and transfer results in `app/auth.py` and `app/ledger.py`.

`db/schema.sql` is idempotent and is applied by seed/tests. Schema edits must preserve repeated application and existing-user behavior; there is no separate migration framework (`app/db.py`, `app/seed.py`, `tests/test_admin_seed.py`).

## Change discipline

`AGENTS.md` requires tests with every code change and forbids deleting, skipping, or weakening tests to make a change pass. Ask before adding dependencies or downloading tools. Keep secrets out of tracked files, logs, and reports.

Before committing, run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`. For fixture patterns and functional QA, see [Testing](testing.md) and [Development workflow](development-workflow.md).
