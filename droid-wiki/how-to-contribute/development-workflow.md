# Development workflow

Use a local Python web process and a disposable Postgres instance, make a focused change with regression coverage, then run the same checks as CI. The branch/PR sequence below is practical guidance, not an enforced repository policy.

## Prepare

Read `AGENTS.md` and follow [Getting started](../overview/getting-started.md). Setup uses `docker compose up -d --wait`, `uv sync`, and `uv run python -m app.seed`; Compose starts only Postgres. Run the application separately:

```sh
uv run uvicorn app.main:app --reload
```

Keep local credentials in `.env`. If changing the Postgres host port, update both `POSTGRES_PORT` and `DATABASE_URL`. Seeding applies the schema but does not reset an already populated database (`README.md`, `docker-compose.yml`, `app/seed.py`).

## Edit and exercise

1. Start a focused working branch if using a PR; no branch naming convention is specified.
2. Change the appropriate layer: ledger rules in `app/ledger.py`, API contracts in `app/routes/api.py`, HTML/form behavior in `app/routes/pages.py` and `app/templates/`.
3. Add or update tests beside the affected behavior. Shared fixtures and helpers are in `tests/conftest.py` and `tests/helpers.py`.
4. Preserve both plain form and htmx submission for UI changes. For authentication changes, check bearer/cookie separation and negative CSRF cases.

`AGENTS.md` requires permission before dependency additions or network downloads. Do not treat an automated suggestion as permission to install tools. Follow [Patterns and conventions](patterns-and-conventions.md).

## Check and submit

During iteration, target a relevant test file, for example:

```sh
uv run pytest tests/test_idempotency.py
```

Even a targeted test invokes the destructive throwaway-database fixtures; read [Testing](testing.md) first. Before committing, run the full required checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`uv run ruff format .` applies formatting; `--check` only verifies it. Review the diff for accidental secrets, unrelated files, and weakened assertions. A useful PR description states the behavior, tests run, and known limitations; this is guidance rather than a checked template.

## Automation feedback

`.github/workflows/ci.yml` runs locked dependency installation, lint, formatting verification, and pytest on pushes to `main` and on pull requests. `.github/workflows/droid-review.yml` reviews non-draft PRs, including new pushes. `.github/workflows/qa.yml` performs separate diff-targeted functional QA when eligible.

Resolve actual failures rather than bypassing tests. Report unavailable checks honestly. The workflow files do not prove that a check is required for merge. See [Tooling](tooling.md) for triggers, credentials, and QA trust boundaries.
