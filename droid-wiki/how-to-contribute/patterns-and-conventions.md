# Patterns and conventions

The rules in `AGENTS.md` protect money movement and authentication boundaries. Match the existing typed Python, SQL parameterization, and route-to-ledger split when changing behavior (`app/ledger.py`, `app/auth.py`, `app/routes/api.py`, `app/routes/pages.py`).

## Money and transactions

- Store and pass integer cents. `Decimal` is only for parsing UI input in `app/routes/pages.py`; do not introduce floating-point money.
- Send ordinary transfers through `execute_transfer` in `app/ledger.py`. Routes must not insert ledger entries.
- Keep the debit, credit, and transfer row in one transaction. `pool.connection()` in `app/ledger.py` commits on success and rolls back on exceptions.
- Lock both accounts in ascending ID order before checking the source balance. Preserve that ordering for all cooperating transfer writers.
- Reject ownerless system funding accounts as transfer endpoints. Direct balanced funding writes in `app/seed.py` and isolated fixtures in `tools/qa_harness.py` are bootstrap/test operations, not a pattern for routes.

The deferred balance trigger in `db/schema.sql` runs at commit, so a successful insert statement alone does not prove the transaction will commit. Preserve tests that inspect committed state and concurrent outcomes. See [Transfers](../features/transfers.md).

`account_balance` in `app/ledger.py` reads a derived balance on an existing connection; `tests/helpers.py` and `tools/qa_harness.py` share it. Callers that adjust a balance must lock the affected account rows before reading it. Harness fixtures lock the complete account batch in ascending ID order and then add balanced adjustments (`tests/test_qa_harness.py`). Keep this local-only exception confined to the [QA harness](../features/qa-harness.md), whose target guards must never be bypassed.

## Input and errors

`app/routes/api.py` uses Pydantic request/response models and bounded headers/query parameters. `app/routes/pages.py` parses form strings explicitly, then renders full HTML or htmx fragments. Both call the same ledger functions instead of copying business rules.

`TransferError` subclasses in `app/ledger.py` define expected transfer failures. The JSON API maps account-not-found errors to 404 and other business errors to 422 (`app/routes/api.py`). The HTML route deliberately maps every caught business error to the same generic 422 message: “We could not complete this transfer. Check the recipient and your balance.” Do not expose the exception text or API status distinctions in browser responses (`app/routes/pages.py`, `tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`). Local form-validation errors remain specific.

Record UI attempts only after CSRF validation, before recipient lookup or money movement. Keep the `transfer_retry_after` upsert in `app/rate_limits.py` in a separate committed transaction so failures and replays count too. Preserve the per-user fixed window across sessions/workers, `429` plus `Retry-After`, and the submitted key on blocked attempts. This limiter does not apply to JSON transfers or login (`app/routes/pages.py`, `tests/test_transfer_rate_limits.py`).

`authenticate` in `app/auth.py` normalizes email and handles database/pool failures with a generic application error. Its log includes only the exception class, not the exception message or traceback. Preserve indistinguishable responses for unknown credentials and ambiguous/missing recipients (`app/auth.py`, `app/ledger.py`).

## Authentication and forms

Keep `get_api_user` and `get_page_user` in `app/auth.py` separate: bearer tokens authenticate JSON; cookies authenticate HTML. `get_api_admin` adds the current database admin check. Never accept a session cookie as an API fallback.

Every form POST needs CSRF, including login and logout. Reuse `require_csrf`, `render`, and the anonymous-token helpers rather than inventing a separate token format (`app/routes/pages.py`, `app/security.py`). See [Security](../security.md).

`app/templates/base.html` enables htmx swapping for error responses. `app/templates/_transfer_result.html` can replace the hidden idempotency key out of band. Keep plain form submission working as well as htmx.

## Python and SQL

`pyproject.toml` targets Python 3.12 with a 100-column Ruff configuration. Selected rule families are E, F, I, B, UP, and SIM; FastAPI dependency/form defaults have an explicit immutable-call exception. `app/static/` is excluded from Ruff.

Use `%s` query parameters for values, as in `app/ledger.py`. Use identifier composition for dynamic identifiers, as in `tests/conftest.py`. Frozen dataclasses carry request user context and transfer results in `app/auth.py` and `app/ledger.py`.

`db/schema.sql` is idempotent and is applied by seed/tests. Schema edits must preserve repeated application and existing-user behavior; there is no separate migration framework (`app/db.py`, `app/seed.py`, `tests/test_admin_seed.py`).

## QA reporting

Keep browser-tool failures separate from application failures. `.factory/skills/qa/scripts/qa-tools.sh` deliberately exits zero for installation and preflight findings so the informational QA job can publish a report. `.factory/skills/qa/SKILL.md` requires reading `qa-results/preflight.txt` on each affected CI web run: missing output or `QA_PREFLIGHT: BLOCKED` blocks browser flows; `APP_FAIL` records are application failures, regardless of the header. Continue affected API flows, and never substitute curl for blocked browser coverage.

Only trusted setup steps install the pinned browser stack. The keyed agent must not run the installer, repair tooling, or execute checkout code. Preserve the offline reporting regressions in `tests/test_qa_tools.py` and the base-branch instruction boundary in `.github/workflows/qa.yml`. See [Testing](testing.md) and [Tooling](tooling.md).

## Change discipline

`AGENTS.md` requires tests with every code change and forbids deleting, skipping, or weakening tests to make a change pass. Ask before adding dependencies or downloading tools. Keep secrets out of tracked files, logs, and reports.

Before committing, run `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest`. For fixture patterns and functional QA, see [Testing](testing.md) and [Development workflow](development-workflow.md).
