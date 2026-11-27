# Configuration

LedgerLite reads five application settings through Pydantic Settings. Compose, pytest, and functional QA have separate configuration responsibilities.

## Application settings

Defined in `app/config.py`:

| Environment variable | Type | Default | Validation and use |
| --- | --- | --- | --- |
| `DATABASE_URL` | string | `postgresql://ledgerlite:ledgerlite@localhost:5432/ledgerlite` | Passed to psycopg; no URL-specific field validator |
| `JWT_SECRET` | string | **Required**, no default | Minimum 16 characters; signs JWTs and CSRF HMACs |
| `JWT_TTL_SECONDS` | integer | `3600` | Token and session-cookie lifetime; no explicit positive/min/max constraint |
| `SEED_PASSWORD` | string | `ledgerlite-dev` | Password used when creating seed users or a new explicit admin; no minimum-length field constraint |
| `PAYMENTS_GATEWAY_API_KEY` | masked string | **Required**, no default | Missing, empty, and whitespace-only values are rejected before seed or startup can connect to the database |

The URL and seed-password defaults are local-development values, not production credentials. `.env.example` contains placeholders and example configuration; copying it does not generate a secret. A long placeholder can satisfy the secret's length validation, so replace it. Keep `DATABASE_URL` credentials, host, port, and database synchronized with your actual Postgres instance.

Settings load `.env` relative to the process working directory. For this configuration, explicit `Settings(...)` arguments override process environment variables, which override `.env` values, which override field defaults. Environment names are case-insensitive by Pydantic Settings' default. Extra dotenv fields are ignored, allowing Compose-only variables to coexist.

`get_settings()` is cached with `lru_cache`. Changing environment variables or `.env` does not update an existing cached object; restart the process, or explicitly clear the cache in controlled test code. There are no separate app settings for port, pool size, cookie security, or log level.

Sources: `app/config.py`, `.env.example`, `app/security.py`, and `app/seed.py`.

## Compose variables

`docker-compose.yml` interpolates these independently of the Python settings:

| Variable | Compose fallback | Effect |
| --- | --- | --- |
| `POSTGRES_USER` | `ledgerlite` | Postgres initialization user |
| `POSTGRES_PASSWORD` | `ledgerlite` | Postgres initialization password |
| `POSTGRES_DB` | `ledgerlite` | Initial database |
| `POSTGRES_PORT` | `5432` | Host port mapped from `127.0.0.1` to container port `5432` |

Compose interpolation uses shell values ahead of its dotenv input; `${VAR:-fallback}` also falls back for an empty value. These variables do not automatically construct or update `DATABASE_URL`. Pydantic reading `.env` does not itself export values to a shell.

The database image is `postgres:16`, with persistent named volume `ledgerlite-pgdata`. Changing initialization credentials does not reinitialize an existing data volume. The readiness check runs every two seconds, with a three-second timeout and 30 retries.

## Fixed runtime choices and command options

`app/db.py` creates a synchronous pool with minimum one and maximum ten connections. `app/main.py` waits up to ten seconds when opening it and closes it on shutdown. Startup does not apply the schema. Schema application is explicit through `app/db.py` and `app/seed.py`.

The documented local server command is `uv run uvicorn app.main:app --reload`; host, port, and logging changes are Uvicorn CLI concerns, not `Settings` fields. Cookie `Secure` is derived from the request hostname in `app/security.py`.

The seed CLI accepts optional `--admin-email <EMAIL>` to create or promote an admin. This is not an environment setting and does not reset an existing user's password.

## Pytest database derivation

`tests/conftest.py` reads a fresh `Settings()` before overriding the cached application configuration:

1. Parse the base database name from `DATABASE_URL`, defaulting the name to `ledgerlite` if absent.
2. Append `_test` unless the name already ends in `_test`.
3. Preserve connection parameters but replace the database name for test connections; use database `postgres` for administrative creation.
4. Export the derived `DATABASE_URL` and clear `get_settings()`'s cache.

The session autouse fixture **drops and recreates that derived database with FORCE**, then applies the schema. Each test truncates and reseeds it. The connection role needs the corresponding administrative privileges. Do not point test configuration at shared or valuable databases.

The fixture sets a test-only `JWT_SECRET` with `setdefault`, so an existing environment value wins. It always uses a dummy `PAYMENTS_GATEWAY_API_KEY` instead of any inherited value. Test seed passwords come from `tests/helpers.py`, not the application's `SEED_PASSWORD`.

## Functional QA configuration

`.factory/skills/qa/scripts/qa-env.sh` requires a `postgres://` or `postgresql://` URL. It retains its scheme, authority, and query string; appends `_qa` to the database name unless already present; defaults an empty name to `ledgerlite`; and exports the rewritten `DATABASE_URL`. `QA_PG_SCHEME`, `QA_PG_USERINFO`, `QA_PG_HOSTPORT`, `QA_PG_DB`, and `QA_PG_QUERY` are derived shell values, not application settings.

`.factory/skills/qa/scripts/qa-server.sh start` requires exported `DATABASE_URL`, `JWT_SECRET`, and `SEED_PASSWORD`, plus an existing virtualenv and `psql`. It drops/recreates the derived QA database and gives the server a generated-password `<qa_database>_app` role without superuser, database/role creation, replication, or RLS-bypass privileges. It owns the QA database.

Optional `QA_RUN_AS` selects an OS user for seed/server commands through noninteractive sudo; otherwise the current user is used. The child environment is scrubbed and explicitly passes the three application values above and a dummy `PAYMENTS_GATEWAY_API_KEY`, but not `JWT_TTL_SECONDS`. The app can still read its working-directory `.env` for other settings.

Port selection starts at 8000 and tries random ports in `20000..39999` if needed. `QA_ROOT`, `QA_PORT`, `QA_NONCE`, `QA_USER`, and `QA_FINGERPRINT` are generated state in `qa-results/.qa-server`, not user-facing app settings. `FACTORY_API_KEY` and `QA_EVIDENCE_TOKEN` must be absent for the lifecycle script's commands; they belong to automation, not the application. Verification checks freshness, not trust.

Persona names, app path patterns, evidence preferences, and the local target are in `.factory/skills/qa/config.yaml`. See [Testing](../how-to-contribute/testing.md) and [Tooling](../how-to-contribute/tooling.md) for the workflow, or [Getting started](../overview/getting-started.md) for local setup.
