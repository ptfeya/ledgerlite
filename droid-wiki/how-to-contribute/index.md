# Contributing

LedgerLite changes should preserve ledger invariants and authentication boundaries, include regression tests, and pass the existing Ruff/pytest checks. `AGENTS.md` is the repository's contribution guidance; the workflow files describe automation, not a complete governance policy.

## Pick up a change

1. Read `AGENTS.md` and the affected implementation before editing.
2. Follow [Getting started](../overview/getting-started.md) for a disposable local environment.
3. Locate the relevant route, ledger function, template, and tests using the [Architecture](../overview/architecture.md).
4. Keep the change focused; ask before adding dependencies or downloading tools.

## Non-negotiable boundaries

- Use integer cents and balanced debit/credit entries in one transaction.
- Route ordinary money movement through `app/ledger.py`; never insert ledger entries from routes.
- Keep the ownerless system funding account out of transfer endpoints.
- Require CSRF on every form POST, including login and logout.
- Accept bearer tokens only on JSON endpoints; do not add cookie fallback.
- Keep `.env`, passwords, JWTs, and CSRF tokens out of commits and reports.
- Add or update tests; never delete, skip, or weaken tests to obtain a pass.

These rules come from `AGENTS.md`. Implementation examples are collected in [Patterns and conventions](patterns-and-conventions.md).

## Review and completion

Before committing, run all three commands required by `AGENTS.md`:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Describe the behavior changed, regression coverage, and any checks blocked by missing prerequisites. Review changes to money movement, ownership, token transport, CSRF, and error confidentiality explicitly. Functional QA can supplement these checks but does not replace them.

The repository has automatic review and informational QA workflows, but their existence does not establish branch protection, required approvals, or a mandatory merge strategy. See [Development workflow](development-workflow.md), [Testing](testing.md), and [Tooling](tooling.md).

Sources: `AGENTS.md`, `.github/workflows/ci.yml`, `.github/workflows/droid-review.yml`, `.github/workflows/qa.yml`.
