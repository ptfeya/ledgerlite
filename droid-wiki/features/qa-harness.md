# QA harness

Active contributors: ptfeya

## Purpose

`tools/qa_harness.py` provides deterministic users and balances, database outages and application restarts for local QA. It manages a separate `<base>_harness` database, `<base>_harness_app` role and Uvicorn process bound to a free loopback port. It does not use the development database, pytest database or the `<base>_qa` instance managed by `.factory/skills/qa/scripts/qa-server.sh`. Sources: `tools/qa_harness.py`, `tools/README.md`.

This is development tooling, not a product endpoint. For the shared application design see [architecture](../overview/architecture.md); for pytest and shell-QA procedures see [testing](../how-to-contribute/testing.md).

## Layout and key abstractions

| Path and abstraction | Responsibility |
| --- | --- |
| `tools/qa_harness.py`: `Target`, `resolve_target` | Derive isolated database/role names and bind saved state to the configured database and server |
| `tools/qa_harness.py`: `_check_local`, `_check_owned` | Check connection locality, agent credentials and database/role markers before managed lifecycle changes |
| `tools/qa_harness.py`: `save_state`, `server_alive`, `start_server`, `stop_server` | Persist run state and manage the loopback application process |
| `tools/qa_harness.py`: `ensure_user`, `set_balance`, `reset_data` | Create/promote users, set exact balances and restore standard seed data |
| `tools/qa_harness.py`: `set_outage`, `outage_active` | Disable/restore database connections and inspect outage state |
| `tools/qa_harness.py`: `build_parser`, `run`, `cmd_up`, `cmd_restart`, `cmd_down` | Dispatch JSON-output lifecycle and fixture commands |
| `app/ledger.py`: `account_balance` | Sum integer-cent entries on an existing connection; shared with `tests/helpers.py` |
| `app/db.py`: `apply_schema`; `app/seed.py`: `seed`, `seed_admin` | Initialize the isolated schema and standard personas |

Runtime files live at `qa-results/.qa-harness.json` and `qa-results/qa-harness-server.log`. The state file is written atomically with mode `0600`. **It contains actual secrets**, including the credential-bearing application database connection string and generated JWT signing key. Never publish, upload or paste the state file. Treat the server log as private diagnostic output and inspect/redact it before sharing. `down` removes both files. Source: `tools/qa_harness.py`.

## Local lifecycle

These commands are for an approved local environment with configured settings and a reachable local Postgres server. Maintenance credentials need permission to manage databases, roles and connections. Do not run checkout Python in keyed CI agents. See [tooling](../how-to-contribute/tooling.md) and [configuration](../reference/configuration.md).

```sh
uv run python -m tools.qa_harness up
uv run python -m tools.qa_harness status
uv run python -m tools.qa_harness fixture user carol@example.com --account Checking=5000
uv run python -m tools.qa_harness fixture user qa-admin@example.com --admin
uv run python -m tools.qa_harness outage on
uv run python -m tools.qa_harness outage status
uv run python -m tools.qa_harness outage off
uv run python -m tools.qa_harness restart
uv run python -m tools.qa_harness fixture reset
uv run python -m tools.qa_harness up --reset
uv run python -m tools.qa_harness down
```

- `up` creates/checks the marked database and restricted role, applies the schema, seeds demo users plus `qa-admin@example.com`, and starts the app. Repeating it while the saved process is alive without `--reset` returns `changed: false`. A fresh setup generates its own database password and JWT signing key.
- `status` with saved state returns `running`, `base_url`, `database`, `outage`, `personas` and a `password` field whose value is the literal string `SEED_PASSWORD`, not the password itself. Without saved state it returns only `running: false`. Contrary to the blanket URL warning in `tools/README.md`, public output does include the local HTTP base URL, but not the credential-bearing database URL.
- `fixture user` accepts repeatable `--account NAME=CENTS` options with non-negative integer cents and unique names. Existing users retain their passwords; `--admin` promotes but never demotes. Repeating unchanged requested balances creates no adjustment entries.
- `outage on` disallows new connections to the isolated database and terminates its existing sessions. The lifecycle regression checks login returns `503` during the outage and recovers after `outage off`. Restore the outage before any fixture command or `restart`.
- `restart` requires saved state bound to the target, not necessarily a currently running process. It retains the database, port and signing key, changes the process nonce and starts a new app process. An unrelated process occupying the saved port causes refusal.
- `fixture reset` truncates ledger entries, transfers, transfer rate limits, accounts and users, resets identities, and restores demo/admin seed data. `up --reset` instead drops and recreates the marked isolated database. Both are destructive to isolated harness data, not shared development/test/QA-server data.
- `down` stops the matching app process, drops the marked database and role, and removes state/log files. Repeating it after cleanup is a no-op. Always clean up when finished.

Source: `tools/qa_harness.py`; regression evidence: `tests/test_qa_harness.py`.

## Fixture funding and integration

Fixtures reuse the application's schema, password hashing and seed functions, then test the same [transfers](transfers.md) and [web interface](web-interface.md) on the isolated app. They do not add a production route or relax endpoint authorization.

`ensure_user` locks all requested accounts plus the system account in increasing account-ID order before adjustments. `set_balance` locks the account/system pair, reads `app/ledger.py`'s `account_balance`, and writes one balanced debit/credit adjustment for the difference. Raising a balance draws from the system funding account; lowering it returns funds there. Each adjustment has a transfer with no owning user; a zero difference writes nothing. This explicit local fixture exception, like opening funding in `app/seed.py`, bypasses ordinary `execute_transfer` rules without permitting system-account transfers through product endpoints. Sources: `tools/qa_harness.py`, `app/ledger.py`, `app/seed.py`.

## Guards and modification

Target derivation strips one recognized `_test`, `_qa` or `_harness` suffix before adding `_harness`. Identifiers are validated. Non-loopback database hosts/addresses and libpq connection services are refused; local Unix socket paths are allowed. Inherited libpq host/address/port settings are checked or pinned where applicable. Nonempty `FACTORY_API_KEY` or `QA_EVIDENCE_TOKEN` causes refusal. Same-named databases or roles without the expected comment markers are refused, including reset and cleanup. Saved state for a different database/server is also refused. Source: `tools/qa_harness.py`.

The command-line/header nonce distinguishes this saved run from a stale or reused PID. It is a freshness/process-matching check, not an authentication or trust boundary; `running` is not a health check. Startup considers any HTTP response from `/ui/login` sufficient to finish waiting. For application auth and guard context see [security](../security.md).

Keep target/marker checks before destructive changes, preserve restricted role creation and scrubbed child environments, and retain global account lock ordering and balanced fixture entries. Extend isolation, fixture and lifecycle regressions when modifying these paths. Tests cover unmarked resources, target binding, inherited connection settings, password retention, concurrent account locks, outage refusal, restart persistence and cleanup. Sources: `tools/qa_harness.py`, `tests/test_qa_harness.py`, `AGENTS.md`.

## Scope and maintenance

Measured at `0c1e05fff5efbae2974eeb32a0f5db0a1ae1210d`: the harness implementation scope is **1 Python file**, `tools/qa_harness.py` (**619 lines**); its dedicated regression scope is **1 module**, `tests/test_qa_harness.py` (**337 lines**). Test-to-code file ratio: **1/1 = 1.00**, not test coverage. The adjacent usage document, `tools/README.md`, has **17 lines** and is excluded from that ratio. Shared application helpers, schema, seeds and pytest fixtures are also excluded.

Git history on `origin/main` for exactly `tools/qa_harness.py` has **one unique human author: ptfeya**, excluding author names containing `[bot]`. This is a scoped history signal, not an ownership guarantee.

## Key source files

| Path | Role |
| --- | --- |
| `tools/qa_harness.py` | Isolation, fixture SQL, process management and command dispatcher |
| `tools/README.md` | Local usage commands and cleanup guidance |
| `tests/test_qa_harness.py` | Guard, fixture, concurrency and full lifecycle regressions |
| `app/ledger.py` | Shared connection-based balance helper and ordinary transfer restrictions |
| `tests/helpers.py` | Pytest balance helper delegating to `account_balance` |
| `app/seed.py` | Demo opening funding and explicit admin seeding |
| `app/db.py` | Shared schema application |
| `db/schema.sql` | Ledger constraints and balanced-entry checks |
| `AGENTS.md` | Harness isolation and contributor execution rules |
