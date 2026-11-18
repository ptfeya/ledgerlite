# Getting started

Run Postgres with Docker Compose, install the Python environment with uv, seed the schema, then start Uvicorn. The commands below follow `README.md`, `AGENTS.md`, and `docker-compose.yml`; they are setup instructions, not commands executed during wiki generation.

## Prerequisites

- Python 3.12 or newer, managed through uv if desired (`pyproject.toml`).
- uv and Docker with Compose v2 (`README.md`).
- A free local database port, normally 5432 (`docker-compose.yml`).

Run commands from the repository root so `.env` and the project configuration resolve correctly (`app/config.py`, `pyproject.toml`).

## Configure and install

```sh
cp .env.example .env
openssl rand -hex 32
```

Put the generated value in `JWT_SECRET` in `.env`. Do not commit that file or paste its contents into logs. `app/config.py` requires a secret of at least 16 characters, but length alone does not make a placeholder safe.

Keep the `POSTGRES_*` values and `DATABASE_URL` consistent. If changing ports, change both `POSTGRES_PORT` and the URL. Choose a local `SEED_PASSWORD` for the demo users (`.env.example`, `docker-compose.yml`, `app/config.py`).

```sh
docker compose up -d --wait
uv sync
uv run python -m app.seed
uv run uvicorn app.main:app --reload
```

Open <http://localhost:8000>. The root redirects to `/ui/accounts`, and a missing session redirects to `/ui/login` (`app/main.py`, `app/routes/pages.py`).

## Demo accounts

| Demo user | Opening accounts |
| --- | --- |
| `alice@example.com` | Checking: $1,000.00; Savings: $500.00 |
| `bob@example.com` | Checking: $750.00; Savings: $250.00 |

Use the password configured by `SEED_PASSWORD`. `app/seed.py` funds these balances with matching negative entries on the system account. Do not assume account IDs in a database that was populated differently; list accounts through the UI or API.

Seeding is safe to repeat, but it is **not a reset**: `seed` in `app/seed.py` skips demo creation whenever any user already exists. Changing `SEED_PASSWORD` does not rotate existing passwords.

For an existing checkout/database, reapply the schema with `uv run python -m app.seed` before using the updated UI transfer route. `db/schema.sql` now includes `transfer_rate_limits`; server startup does not create it. Schema reapplication preserves existing rate-limit counters as well as ledger data (`app/db.py`, `app/seed.py`, `tests/test_transfer_rate_limits.py`).

## Optional local admin

```sh
uv run python -m app.seed --admin-email admin@example.com
```

`seed_admin` in `app/seed.py` creates or promotes that user. A new admin has no accounts and receives the seed password; promotion preserves an existing password and accounts. Ordinary seeding neither grants nor revokes admin access.

## Optional isolated QA

With Postgres and the approved Python environment ready, `tools/qa_harness.py` can run a separate local instance:

```sh
uv run python -m tools.qa_harness up
uv run python -m tools.qa_harness status
# Use the returned base_url for local requests.
uv run python -m tools.qa_harness down
```

This uses `<base>_harness`, not the development database or the shell QA server's `<base>_qa`. Run it only locally, without `FACTORY_API_KEY` or `QA_EVIDENCE_TOKEN` in the environment. Do not bypass that guard in a keyed CI agent. Always finish with `down`; see [QA harness](../features/qa-harness.md) and `tools/README.md` for fixtures, outages, and reset semantics.

## Browser QA prerequisites

For local functional browser QA, the approved browser tooling must already be installed. `.factory/skills/qa/SKILL.md` checks `agent-browser --version` and `agent-browser doctor --offline --quick`; a failure means browser coverage is BLOCKED. The CI installer in `.factory/skills/qa/scripts/qa-tools.sh` is not a command for a secret-holding QA agent. Ask before downloads, as required by `AGENTS.md`; see [Tooling](../how-to-contribute/tooling.md).

## Run checks

Keep Postgres running, then use the existing checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`tests/conftest.py` recreates a database derived from `DATABASE_URL`, normally `ledgerlite_test`. It needs database-creation privileges and must run only on a disposable local/test server. It resets data before each test. Do not point tests at a shared or production Postgres instance.

See [Testing](../how-to-contribute/testing.md), [Configuration](../reference/configuration.md), and [Debugging](../how-to-contribute/debugging.md) for details.
