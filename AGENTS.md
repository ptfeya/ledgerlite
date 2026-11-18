# LedgerLite

Payments and transfers service on a double-entry ledger.
Python 3.12, uv, FastAPI, Postgres 16 (Docker Compose), psycopg, Jinja2 + htmx, pytest.

## Commands

```sh
cp .env.example .env                    # then set JWT_SECRET: openssl rand -hex 32
docker compose up -d --wait             # Postgres on 127.0.0.1:5432
uv sync                                 # install dependencies
uv run python -m app.seed               # create schema and demo data (safe to re-run)
uv run uvicorn app.main:app --reload    # run at http://localhost:8000
uv run pytest                           # test (needs the Compose Postgres)
uv run ruff check .                     # lint
uv run ruff format .                    # format
```

Seeded logins: `alice@example.com` and `bob@example.com`, password `SEED_PASSWORD` from `.env`.
Tests use a separate `<POSTGRES_DB>_test` database and never touch dev data.

## Layout

- `app/ledger.py`: all money movement and ledger queries
- `app/routes/api.py`: JSON API (bearer token auth)
- `app/routes/pages.py`: HTML pages under `/ui` (cookie session, CSRF)
- `app/auth.py`, `app/security.py`: auth dependencies, JWT, passwords, CSRF tokens, cookies
- `app/seed.py`, `app/config.py`, `app/db.py`, `app/main.py`: seeding, settings, pool, app setup
- `app/templates/`, `app/static/`: Jinja2 templates, CSS, vendored htmx
- `db/schema.sql`: tables, constraints, triggers (idempotent, safe to re-apply)
- `tools/qa_harness.py`: isolated fixture/outage/restart harness for local QA (usage in `tools/README.md`)
- `tests/`: pytest suite; shared helpers in `tests/helpers.py`

## Rules

- Amounts are stored and passed as integer cents. `Decimal` is only for parsing user input in `app/routes/pages.py`.
- Every transfer must net to zero: a debit and a credit of equal size in one transaction.
- All transfers go through `app/ledger.py`. Do not write to `ledger_entries` from routes.
- The system funding account has no owner and must never be a transfer source or destination.
- Every form POST needs a CSRF token, including login and logout.
- The JSON API accepts bearer tokens only. Never accept the session cookie there.
- The QA harness only touches its own `<POSTGRES_DB>_harness` database and refuses anything else. Never point it at dev, test or QA-server data.
- Never commit `.env`. Use `.env.example` for new settings.
- Add or update tests with every change.
- Never delete, skip or weaken a test to make it pass. Fix the code or ask.
- Ask before adding a dependency or downloading anything from the network.
- Run `uv run ruff check .`, `uv run ruff format --check .` and `uv run pytest` before committing.
