# Testing

LedgerLite has database-backed pytest regressions and Ruff checks, plus a separate agent-driven functional QA system. Both test paths use disposable databases, but their lifecycle and privileges differ.

## Existing automated checks

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

`pyproject.toml` configures pytest to discover `tests/`, add the repository to Python's import path, and show extra summary information with `-ra`. Ruff targets Python 3.12, 100 columns, and E/F/I/B/UP/SIM rules; `app/static/` is excluded. `.github/workflows/ci.yml` runs these checks with Postgres 16 and `uv sync --locked`.

## Exactly what pytest resets

`tests/conftest.py` reads the base connection from `Settings().database_url`, not directly from `POSTGRES_DB`. It parses the database name, falling back to `ledgerlite`; appends `_test` unless already present; and retains the server and credentials. Thus `DATABASE_URL` ending in `/custom` selects `custom_test`, while `/custom_test` remains unchanged.

The session-autouse fixture connects to the same server's `postgres` database, force-drops the derived test database, recreates it, and applies `db/schema.sql`. It does not register a teardown drop. Before every test, another autouse fixture truncates the four application tables, restarts identities, and seeds Alice, Bob, and `admin@example.com`.

Use only a disposable local/CI Postgres server with sufficient database-creation and test DDL privileges. Never aim this suite at a valuable database merely because its name ends in `_test`. The suffix is selection logic, not a security boundary.

## Fixture and assertion patterns

| Fixture/helper | Purpose |
| --- | --- |
| `db` | Autocommit, dictionary-row connection for setup/assertions |
| `pool` | Real pool opened for direct ledger/concurrency tests |
| `client` | FastAPI `TestClient` with lifespan and `http://localhost` cookie behavior |
| `ids` | Resolve seeded account IDs by owner/name rather than hard-code them |
| `alice`, `bob`, `admin` | Bearer authorization dictionaries from API login |
| `balance`, `ledger_total`, `count` | Inspect committed amounts and row counts |
| `post_transfer`, `transfer_form`, `page_login`, `csrf_from_html` | Build requests consistently with the real transports |

Sources: `tests/conftest.py`, `tests/helpers.py`.

Check balance deltas, total ledger sum, entry counts, and unchanged state on failure—not just response codes. `tests/test_recipient_lookup.py` uses mocks and an isolated FastAPI app to verify route delegation, but the global autouse fixtures still require Postgres. It is not a standalone database-free suite.

## Regression map

| Area | Repo-root test paths |
| --- | --- |
| Ownership, bearer-only API, admin changes/deletion | `tests/test_access_control.py` |
| Replay, conflicting payload, rollback/key reuse, concurrent duplicate requests | `tests/test_idempotency.py` |
| Overdraft, exact balance, concurrent spending, invalid endpoints | `tests/test_overdraft.py` |
| Zero sum, opposite-direction concurrency, deferred trigger, append-only ledger | `tests/test_ledger_zero_sum.py` |
| Funding ownership and forbidden transfer endpoints | `tests/test_system_account.py` |
| Browser flow, email recipient, currency, replay, CSRF, cookie flags | `tests/test_pages_and_csrf.py` |
| Mocked recipient lookup and route delegation | `tests/test_recipient_lookup.py` |
| Generic login failures, safe logs, missing fields, retry | `tests/test_login_errors.py` |
| UI lookup/transfer database failures, full HTML and htmx retry | `tests/test_transfer_errors.py` |
| Schema reapplication and explicit admin seeding | `tests/test_admin_seed.py` |
| Disabled public API docs/schema routes | `tests/test_api_docs_disabled.py` |

Concurrency tests call `execute_transfer` through real pooled connections and thread executors. Opposite-direction transfers exercise lock ordering; concurrent spending and same-key tests check committed outcomes. Fault-injection tests are scoped simulations, not proof of every operational failure mode.

## Functional QA is a different layer

`.factory/skills/qa/SKILL.md` explicitly excludes pytest, Ruff, and static analysis. It maps the diff to API/web apps using `.factory/skills/qa/config.yaml`, then selects relevant flows from `.factory/skills/qa-api/SKILL.md` and `.factory/skills/qa-web/SKILL.md`. Dependency changes affect both apps; tests/docs/workflow-only changes do not by themselves select an app.

API flows use HTTP requests; web flows use the droid-control browser route. Personas are Alice, Bob, and `qa-admin@example.com`. Include changed behavior, adjacent integration behavior, and a negative case; report blocked prerequisites rather than silently skipping them. No affected app means INCONCLUSIVE, not PASS.

### QA database and freshness

`.factory/skills/qa/scripts/qa-env.sh` requires a PostgreSQL URL and appends `_qa` to its database name unless already present, preserving connection authority and query parameters. An absent database name falls back to `ledgerlite`. `qa-server.sh start` then force-recreates that database, creates/updates `<qa-db>_app` with a random password and restricted role attributes, and seeds the demo plus QA admin. The app receives that role's URL, not the administrative URL.

The starter needs database/role administration privileges, `psql`, a prepared `.venv`, `JWT_SECRET`, and `SEED_PASSWORD`. It records checkout path, selected port, OS user, nonce, and a SHA-256 fingerprint of `HEAD` plus `git diff HEAD`. Untracked contents are not included in that fingerprint.

Before reusing the server, use the prescribed credential-free verifier:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent .factory/skills/qa/scripts/qa-server.sh verify
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent .factory/skills/qa/scripts/qa-server.sh url
```

Verification checks checkout/fingerprint, one matching process owned by the recorded user with the exact command line, and the HTTP `x-qa-nonce` response. This establishes freshness, not trust or correctness. Do not reuse an arbitrary service on port 8000.

In CI the keyed agent must never start the application or execute checkout Python/venv code. The workflow starts it beforehand as another OS user. See [Tooling](tooling.md) for that boundary and evidence handling.

## Validation scope of this documentation

This wiki run checked sources and generated documentation only. uv, `.venv`, and capture drivers were unavailable; no dependencies were installed and no pytest, Ruff, application, or functional QA run is claimed. See [Debugging](debugging.md) when preparing a real run.
