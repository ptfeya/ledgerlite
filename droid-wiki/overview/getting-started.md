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

Set `PAYMENTS_GATEWAY_API_KEY` before seeding or starting the app. It must not be blank. Use a dummy value only in isolated development or testing; use approved secret injection in other environments.

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

## Optional local admin

```sh
uv run python -m app.seed --admin-email admin@example.com
```

`seed_admin` in `app/seed.py` creates or promotes that user. A new admin has no accounts and receives the seed password; promotion preserves an existing password and accounts. Ordinary seeding neither grants nor revokes admin access.

## Run checks

Keep Postgres running, then use the existing checks:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`tests/conftest.py` recreates a database derived from `DATABASE_URL`, normally `ledgerlite_test`. It needs database-creation privileges and must run only on a disposable local/test server. It resets data before each test. Do not point tests at a shared or production Postgres instance.

See [Testing](../how-to-contribute/testing.md), [Configuration](../reference/configuration.md), and [Debugging](../how-to-contribute/debugging.md) for details.
