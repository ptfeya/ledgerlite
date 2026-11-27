# Configuration

LedgerLite reads five application settings through Pydantic Settings; two of them, `JWT_SECRET` and `PAYMENTS_GATEWAY_API_KEY`, are required. Compose, pytest, and functional QA have separate configuration responsibilities.

## Application settings

Defined in `app/config.py`:

| Environment variable | Type | Default | Validation and use |
| --- | --- | --- | --- |
| `DATABASE_URL` | string | `postgresql://ledgerlite:ledgerlite@localhost:5432/ledgerlite` | Passed to psycopg; no URL-specific field validator |
| `JWT_SECRET` | string | **Required**, no default | Minimum 16 characters; signs JWTs and CSRF HMACs |
| `JWT_TTL_SECONDS` | integer | `3600` | Token and session-cookie lifetime; no explicit positive/min/max constraint |
| `SEED_PASSWORD` | string | `ledgerlite-dev` | Password used when creating seed users or a new explicit admin; no minimum-length field constraint |
| `PAYMENTS_GATEWAY_API_KEY` | `SecretStr` | **Required**, no default | Missing, empty and whitespace-only values fail validation (`require_nonblank_gateway_key`); masked in `repr` and JSON dumps; not otherwise read by repository code |

The URL and seed-password defaults are local-development values, not production credentials. `JWT_SECRET` and `PAYMENTS_GATEWAY_API_KEY` have no defaults, so both must be set before `app/seed.py` or `app/main.py` can load settings. `.env.example` lists the gateway key with an empty value; leave it empty and you get a validation error. Use a dummy gateway value only for isolated local development or testing, and inject the real credential through an approved secret store elsewhere (`README.md`). The setting's earlier hardcoded default was removed in [commit e8d13cf](https://github.com/ptfeya/ledgerlite/commit/e8d13cf56454d987fd6f0f8000a60f4883c4f78f) but remains in Git history; see [Security](../security.md#known-gaps). Copying `.env.example` does not generate a secret. A long placeholder can satisfy the JWT secret's length validation, so replace it. Keep `DATABASE_URL` credentials, host, port, and database synchronized with your actual Postgres instance.

`Settings` sets `hide_input_in_errors=True`, so validation errors name the failing field and reason (for example `Field required` or `must not be blank`) without echoing the submitted value.

Settings load `.env` relative to the process working directory. For this configuration, explicit `Settings(...)` arguments override process environment variables, which override `.env` values, which override field defaults. Environment names are case-insensitive by Pydantic Settings' default. Extra dotenv fields are ignored, allowing Compose-only variables to coexist.

`get_settings()` is cached with `lru_cache`. Changing environment variables or `.env` does not update an existing cached object; restart the process, or explicitly clear the cache in controlled test code. There are no separate app settings for port, pool size, cookie security, or log level.

Sources: `app/config.py`, `.env.example`, `README.md`, `app/security.py`, `app/seed.py`, and `tests/test_gateway_config.py`.

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

`app/rate_limits.py` fixes `TRANSFER_ATTEMPTS = 20` and `TRANSFER_WINDOW_SECONDS = 60` in code, not environment settings. Only `POST /ui/transfer` calls the limiter after CSRF validation. Counters persist in the `transfer_rate_limits` table defined in `db/schema.sql`; changing workers or sessions does not reset them. See [Web interface](../features/web-interface.md) for counted attempts and response behavior.

The documented local server command is `uv run uvicorn app.main:app --reload`; host, port, and logging changes are Uvicorn CLI concerns, not `Settings` fields. Cookie `Secure` is derived from the request hostname in `app/security.py`.

The seed CLI accepts optional `--admin-email <EMAIL>` to create or promote an admin. This is not an environment setting and does not reset an existing user's password.

## Pytest database derivation

`tests/conftest.py` reads a fresh `Settings()` before overriding the cached application configuration:

1. Parse the base database name from `DATABASE_URL`, defaulting the name to `ledgerlite` if absent.
2. Append `_test` unless the name already ends in `_test`.
3. Preserve connection parameters but replace the database name for test connections; use database `postgres` for administrative creation.
4. Export the derived `DATABASE_URL` and clear `get_settings()`'s cache.

The session autouse fixture **drops and recreates that derived database with FORCE**, then applies the schema. Each test truncates and reseeds it. The connection role needs the corresponding administrative privileges. Do not point test configuration at shared or valuable databases.

The fixture sets a test-only `JWT_SECRET` with `setdefault`, so an existing environment value wins. The gateway key is handled differently: `tests/conftest.py` deletes every inherited environment variable whose upper-cased name is `PAYMENTS_GATEWAY_API_KEY`, then sets a fixed test-only value, so a developer's real key never reaches the suite. Deleting all case variants matters because Pydantic's environment lookup is case-insensitive (`tests/test_gateway_config.py`). Test seed passwords come from `tests/helpers.py`, not the application's `SEED_PASSWORD`.
## Functional QA configuration

### Local harness configuration

`tools/qa_harness.py` derives its base from `get_settings().database_url`, not automatically from `POSTGRES_DB`. It parses the database name, defaults an absent name to `ledgerlite`, strips **one** matching suffix in the order `_harness`, `_test`, `_qa`, then appends `_harness`; the role is `<base>_harness_app`. Both derived identifiers must match `^[a-z][a-z0-9_]{0,40}$`. Administrative operations use database `postgres` on that configured server.

Allowed host/address entries are `localhost`, `127.0.0.1`, `::1` or absolute Unix socket paths. Connection-string `host`/`hostaddr` and inherited `PGHOST`/`PGHOSTADDR` are considered, with explicit values taking precedence for the corresponding field. Connection services (`service` or nonempty `PGSERVICE`) are refused. Inherited host/address/port defaults, including `PGPORT`, are pinned into connection strings where applicable so the scrubbed child uses the same server. Nonempty `FACTORY_API_KEY` or `QA_EVIDENCE_TOKEN` causes refusal.

The harness generates its own role password, JWT signing key and process nonce; these are runtime state, not new application settings. It starts Uvicorn at `127.0.0.1` on a free port; saved-state status advertises `base_url` using `localhost` and labels the password with the literal `SEED_PASSWORD`, never its value. Restart retains the saved port and signing key.

The child environment explicitly passes `PATH`, `HOME`, `PYTHONDONTWRITEBYTECODE`, generated `DATABASE_URL`/`JWT_SECRET`, configured `SEED_PASSWORD` and a fixed dummy `PAYMENTS_GATEWAY_API_KEY`. It does not pass `JWT_TTL_SECONDS`, but runs at the repository root, so `app/config.py` still loads `.env` fallback settings. Environment scrubbing is not a claim that dotenv input is disabled. The harness process itself calls `get_settings()`, so it needs a nonblank gateway value (a dummy in `.env` or the environment) before it can start (`tools/README.md`).

Private `qa-results/.qa-harness.json` stores the credential-bearing app connection string and signing key, plus target/server, port, PID and nonce. It is written with mode `0600` and must never be published or uploaded. The log is `qa-results/qa-harness-server.log`. Saved target mismatches and same-named unmarked database/role resources are refused. See [QA harness](../features/qa-harness.md) for commands and [Debugging](../how-to-contribute/debugging.md) for recovery; this local harness remains separate from the shell QA configuration below.

`.factory/skills/qa/scripts/qa-env.sh` requires a `postgres://` or `postgresql://` URL. It retains its scheme, authority, and query string; appends `_qa` to the database name unless already present; defaults an empty name to `ledgerlite`; and exports the rewritten `DATABASE_URL`. `QA_PG_SCHEME`, `QA_PG_USERINFO`, `QA_PG_HOSTPORT`, `QA_PG_DB`, and `QA_PG_QUERY` are derived shell values, not application settings.

`.factory/skills/qa/scripts/qa-server.sh start` requires exported `DATABASE_URL`, `JWT_SECRET`, and `SEED_PASSWORD`, plus an existing virtualenv and `psql`. It drops/recreates the derived QA database and gives the server a generated-password `<qa_database>_app` role without superuser, database/role creation, replication, or RLS-bypass privileges. It owns the QA database.

Optional `QA_RUN_AS` selects an OS user for seed/server commands through noninteractive sudo; otherwise the current user is used. The child environment is scrubbed and explicitly passes the three application values above plus a fixed dummy `PAYMENTS_GATEWAY_API_KEY`, never one inherited from the parent environment. It does not pass `JWT_TTL_SECONDS`. The app can still read its working-directory `.env` for other settings. In CI, `.github/workflows/qa.yml` also hard-links a dotenv file containing only the dummy gateway key to `.env` (failing if `.env` already exists), because the trusted base-branch launcher may not yet forward the new setting.

Port selection starts at 8000 and tries random ports in `20000..39999` if needed. `QA_ROOT`, `QA_PORT`, `QA_NONCE`, `QA_USER`, and `QA_FINGERPRINT` are generated state in `qa-results/.qa-server`, not user-facing app settings. `FACTORY_API_KEY` and `QA_EVIDENCE_TOKEN` must be absent for the lifecycle script's commands; they belong to automation, not the application. Verification checks freshness, not trust.

Persona names, app path patterns, evidence preferences, and the local target are in `.factory/skills/qa/config.yaml`. See [Testing](../how-to-contribute/testing.md) and [Tooling](../how-to-contribute/tooling.md) for the workflow, or [Getting started](../overview/getting-started.md) for local setup.
