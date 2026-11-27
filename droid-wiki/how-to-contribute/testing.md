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

The session-autouse fixture connects to the same server's `postgres` database, force-drops the derived test database, recreates it, and applies `db/schema.sql`. It does not register a teardown drop. Before every test, another autouse fixture truncates the four ledger/identity tables with `CASCADE`, which also clears the fifth table, `transfer_rate_limits`; it restarts identities and seeds Alice, Bob, and `admin@example.com`.

Use only a disposable local/CI Postgres server with sufficient database-creation and test DDL privileges. Never aim this suite at a valuable database merely because its name ends in `_test`. The suffix is selection logic, not a security boundary.

Before importing the app, `tests/conftest.py` sets a test-only `JWT_SECRET` (with `setdefault`, so an existing value wins) and forces `PAYMENTS_GATEWAY_API_KEY` to a fixed test-only value after deleting every inherited case variant of that name. You do not need a gateway key to run the suite, and a real one in your shell is never used. `tests/test_gateway_config.py` checks this in a subprocess and also exercises `Settings` and the app lifespan directly with `get_settings.cache_clear()` and a temporary working directory.

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

`tests/helpers.py`'s `balance` now delegates to the connection-based `account_balance` in `app/ledger.py`. The local harness reuses the same sum-of-entries helper; neither helper stores balances or acquires account locks itself.

Check balance deltas, total ledger sum, entry counts, and unchanged state on failure—not just response codes. `tests/test_recipient_lookup.py` uses mocks and an isolated FastAPI app to verify route delegation, but the global autouse fixtures still require Postgres. It is not a standalone database-free suite.

## Regression map

| Area | Repo-root test paths |
| --- | --- |
| Ownership, bearer-only API, admin changes/deletion | `tests/test_access_control.py` |
| Replay, conflicting payload, rollback/key reuse, concurrent duplicate requests | `tests/test_idempotency.py` |
| Overdraft, exact balance, concurrent spending, invalid endpoints | `tests/test_overdraft.py` |
| Zero sum, opposite-direction concurrency, deferred trigger, append-only ledger | `tests/test_ledger_zero_sum.py` |
| Funding ownership and forbidden transfer endpoints | `tests/test_system_account.py` |
| Browser flow, email recipient, currency, replay, CSRF (including multipart-encoded login), cookie flags | `tests/test_pages_and_csrf.py` |
| Mocked recipient lookup, route delegation, identical business-error responses, throttle ordering and safe throttle outages | `tests/test_recipient_lookup.py` |
| Generic login failures, safe logs, missing fields, retry | `tests/test_login_errors.py` |
| UI lookup/transfer database failures, full HTML and htmx retry, unknown-recipient/overdraft response equivalence | `tests/test_transfer_errors.py` |
| Atomic cross-pool limits, per-user/session behavior, expiry, schema reapplication, failures/replays and CSRF exclusion | `tests/test_transfer_rate_limits.py` |
| Schema reapplication and explicit admin seeding | `tests/test_admin_seed.py` |
| Required, nonblank, masked gateway key; startup fails before building the pool; environment beats `.env`; conftest dummy wins over inherited case variants | `tests/test_gateway_config.py` |
| Disabled public API docs/schema routes | `tests/test_api_docs_disabled.py` |
| Harness target/locality/libpq/credential/marker guards, exact fixtures, ordered locks, private state, outage and restart lifecycle | `tests/test_qa_harness.py` |
| Browser-tool installation/reporting contract, missing tools/pins, browser launch and application rendering failures | `tests/test_qa_tools.py` |

### Platform skips

`tests/test_qa_harness.py` and `tests/test_qa_tools.py` each set a module-level `pytestmark = pytest.mark.skipif(sys.platform == "darwin", ...)` with the reason "Requires Linux (Bash 4+ and the Linux Chrome build)." On macOS every test in those two modules is reported as skipped, and the `-ra` summary prints that reason. Every other module still runs there. CI runs `uv run pytest` on `ubuntu-24.04` (`.github/workflows/ci.yml`), so both modules execute in CI. A green local run on macOS says nothing about the [QA harness](../features/qa-harness.md) or the browser-tool reporting contract; rely on Linux or CI results when changing `tools/qa_harness.py` or `.factory/skills/qa/scripts/qa-tools.sh`.

Concurrency tests call `execute_transfer` through real pooled connections and thread executors. Opposite-direction transfers exercise lock ordering; concurrent spending and same-key tests check committed outcomes. Fault-injection tests are scoped simulations, not proof of every operational failure mode.

`tests/test_transfer_rate_limits.py` sends 40 concurrent attempts through two pools and requires exactly 20 allowed attempts. It also tests saturation at 21, non-extending blocked windows, expiry reset, and persistent counters across schema application and fresh app sessions. UI integration tests check no lookup or money movement while blocked, `Retry-After`, preserved form keys, and both full-page and htmx results. Response-equivalence tests compare status, body and headers, not timing (`tests/test_recipient_lookup.py`, `tests/test_transfer_errors.py`).

## Functional QA is a different layer

### Browser-tool reporting regressions

`tests/test_qa_tools.py` copies `.factory/skills/qa/scripts/qa-tools.sh` and its trusted manifests into temporary storage and supplies stub executables. It checks healthy preflight, missing Chrome/plugin, recorded install failures, browser launch failure, unreadable pins, HTTP 500/missing login password field, exported tool locations, and refusal to honor path/output overrides. Installation and browser behavior are simulated without downloads or a real browser. The global autouse fixtures in `tests/conftest.py` still recreate and seed Postgres, so even `uv run pytest tests/test_qa_tools.py` is not database-free.

These tests validate the reporting contract, not a successful real installation or browser session. See [Tooling](tooling.md) for the pinned stack and trusted setup.

### Local harness regressions

`tests/test_qa_harness.py` checks target derivation from recognized database suffixes; local host/address and inherited libpq/service guards; forbidden agent credentials; unsafe identifiers; and refusal of unmarked databases/roles or mismatched saved targets. Its guard functions may not query Postgres, but global autouse fixtures in `tests/conftest.py` still recreate and seed the test database. Do not describe these as database-free tests.

Fixture regressions verify exact non-negative integer-cent balances, balanced adjustments in both directions, repeat-command stability, unchanged existing passwords and promotion without demotion. Threaded cases check an adjustment waits for an in-flight account lock and a multi-account batch takes locks in global account-ID order.

The lifecycle fixture uses **`ledgerlite_pytest_harness`** on the configured administrative server, with state/log paths redirected to pytest temporary storage and `cmd_down` cleanup. It starts a real isolated Uvicorn process, verifies state mode `0600` and an independently generated signing key, exercises fixture login and database outage `503`/recovery, refuses restart during outage, and checks data, port and JWT signing key survive restart. It also verifies repeated startup/cleanup and removal of the database, role and state. These are regression assertions, not results of this documentation run.

Keep the layers distinct: ordinary pytest uses its derived `_test` database; shell functional QA uses `_qa`; the local [QA harness](../features/qa-harness.md) uses a marked `_harness` database. The special lifecycle fixture is separate from the ordinary pytest database. Harness commands are not a keyed-agent replacement for the shell-QA startup path.

`.factory/skills/qa/SKILL.md` explicitly excludes pytest, Ruff, and static analysis. It maps the diff to API/web apps using `.factory/skills/qa/config.yaml`, then selects relevant flows from `.factory/skills/qa-api/SKILL.md` and `.factory/skills/qa-web/SKILL.md`. Dependency changes affect both apps; tests/docs/workflow-only changes do not by themselves select an app.

API flows use HTTP requests; web flows use the droid-control browser route. Personas are Alice, Bob, and `qa-admin@example.com`. Include changed behavior, adjacent integration behavior, and a negative case; report blocked prerequisites rather than silently skipping them. No affected app means INCONCLUSIVE, not PASS.

### QA database and freshness

`.factory/skills/qa/scripts/qa-env.sh` requires a PostgreSQL URL and appends `_qa` to its database name unless already present, preserving connection authority and query parameters. An absent database name falls back to `ledgerlite`. `qa-server.sh start` then force-recreates that database, creates/updates `<qa-db>_app` with a random password and restricted role attributes, and seeds the demo plus QA admin. The app receives that role's URL, not the administrative URL.

The starter needs database/role administration privileges, `psql`, a prepared `.venv`, `JWT_SECRET`, and `SEED_PASSWORD`. It passes the app a fixed dummy `PAYMENTS_GATEWAY_API_KEY`, never a value from the parent environment. It records checkout path, selected port, OS user, nonce, and a SHA-256 fingerprint of `HEAD` plus `git diff HEAD`. Untracked contents are not included in that fingerprint.

Before reusing the server, use the prescribed credential-free verifier:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent .factory/skills/qa/scripts/qa-server.sh verify
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent .factory/skills/qa/scripts/qa-server.sh url
```

Verification checks checkout/fingerprint, one matching process owned by the recorded user with the exact command line, and the HTTP `x-qa-nonce` response. This establishes freshness, not trust or correctness. Do not reuse an arbitrary service on port 8000.

In CI the keyed agent must never start the application or execute checkout Python/venv code. The workflow starts it beforehand as another OS user. See [Tooling](tooling.md) for that boundary and evidence handling.

### Browser preflight and report contract

For each affected web run in CI, `.factory/skills/qa/SKILL.md` and `.factory/skills/qa-web/SKILL.md` require reading `qa-results/preflight.txt`, produced before the keyed agent by trusted `.factory/skills/qa/scripts/qa-tools.sh`:

| Preflight evidence | Required report outcome |
| --- | --- |
| `QA_PREFLIGHT: OK` without application findings | Continue selected web flows; this is readiness, not a flow PASS. |
| `QA_PREFLIGHT: BLOCKED` with `BLOCKED <check>: <reason>` | Mark every affected web flow BLOCKED, quote reasons in Notes, and name the failed check in Action Required. Continue affected API flows. |
| Missing or unreadable file | BLOCKED, with reason “preflight did not run”. |
| `APP_FAIL <check>: <reason>` under either header | Mark affected web flows FAIL, never BLOCKED; continue web flows the page permits. An OK header does not cancel application findings. |

The script first renders a static data page independent of the app; launch/render failures there are tooling BLOCKED. Only after that succeeds does it inspect `/ui/login`; HTTP or password-field failures there are APP_FAIL. Both install and preflight deliberately return zero, so the report is authoritative, not the shell exit code. The agent must not run the installer, repair tools, or replace blocked browser flows with curl. Locally, the skill instead checks `agent-browser --version` and `agent-browser doctor --offline --quick`.

## Validation scope of this documentation

This incremental wiki update reads sources and edits documentation only. No dependencies were installed, no application code was executed, and no pytest, Ruff or functional QA result is claimed. Runtime tool availability is not established by these pages. Inline wiki validations are handled by the main documentation agent and must be reported separately from runtime checks. See [Debugging](debugging.md) when preparing a real run.
