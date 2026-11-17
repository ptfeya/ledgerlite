# Deployment

LedgerLite documents a local FastAPI/Uvicorn process backed by Postgres 16. Docker Compose starts the database only. There is no application image, production deployment pipeline, preview environment, or hosting configuration in the supplied repository.

## Supported local topology

```text
Browser / API client
        |
        v
Uvicorn: app.main:app
  JSON routes + /ui + /static
        |
        v
Postgres 16 (Compose db)
  named volume: ledgerlite-pgdata
```

`docker-compose.yml` binds Postgres to `127.0.0.1:${POSTGRES_PORT:-5432}` and persists its data in a named volume. Its `pg_isready` healthcheck confirms database readiness, not application correctness. There are no worker or queue services.

`app/main.py` mounts static assets, includes both route layers, and opens a synchronous psycopg pool during lifespan. `app/db.py` configures 1–10 connections; startup waits up to 10 seconds, and shutdown closes the pool.

## Bootstrap and run

Follow [Getting started](overview/getting-started.md) to create a private `.env` and install approved prerequisites. The documented sequence is:

```sh
docker compose up -d --wait
uv sync
uv run python -m app.seed
uv run uvicorn app.main:app --reload
```

These are local setup instructions, not commands executed during this wiki run. `--reload` is the development command, not a production process-management recommendation.

`app/config.py` reads `.env` and environment settings and caches the settings instance. `JWT_SECRET` is required; keep it private. Align `DATABASE_URL` with Compose's database credentials and host port. See [Configuration](reference/configuration.md) for defaults and precedence.

## Schema, seed, and persistence

The server does not create or migrate tables on startup. `app/seed.py` explicitly calls `apply_schema` from `app/db.py`, which executes `db/schema.sql`. The schema supports repeated application; there is no versioned migration runner.

Demo creation skips any database already containing users. Re-running seed therefore does not reset balances or rotate passwords. `--admin-email` explicitly creates/promotes an admin and preserves an existing user's password/accounts. These are development bootstrap operations, not an automatic release migration strategy.

Normal Compose shutdown retains the named database volume. Do not delete volumes or run test resets against valuable data as a troubleshooting shortcut. Pytest recreates a derived `_test` database; functional QA recreates a derived `_qa` database. Both require disposable environments, as detailed in [Testing](how-to-contribute/testing.md).

## CI is not application deployment

`.github/workflows/ci.yml` supplies an ephemeral Postgres service and runs lint, formatting verification, and pytest. `.github/workflows/qa.yml` starts a temporary application server and performs functional HTTP/browser checks against candidate code. Its server uses a limited database role and, in CI, a separate OS user from the secret-holding agent.

`.github/workflows/droid.yml` and `.github/workflows/droid-review.yml` provide tagged assistance and automatic review. None ships the runtime to a production host.

`.github/workflows/droid-wiki-refresh.yml` publishes documentation, not the application: a read-only generation job produces an artifact; a separate job with write permissions updates a rolling wiki PR. `FACTORY_API_KEY` is used for generation; optional `FACTORY_WIKI_PR_TOKEN` or built-in `GITHUB_TOKEN` is used for the PR. Conditional QA evidence uses `QA_EVIDENCE_TOKEN`. See [Tooling](how-to-contribute/tooling.md) for all five workflows and their limitations.

## Production work not supplied

A production rollout would need separately designed hosting/process supervision, TLS and proxy configuration, secret delivery/rotation, restricted database credentials, schema rollout/rollback, backup/restore verification, and operational monitoring. The repository does not supply those procedures or a supported production command.

Do not infer high availability, zero-downtime upgrades, managed backups, or automated rollback from local Compose persistence or passing CI. Available observability and application security boundaries are described in [Monitoring](how-to-monitor.md) and [Security](security.md).
