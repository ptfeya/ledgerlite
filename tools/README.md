# QA fixture and lifecycle harness

`tools/qa_harness.py` gives QA agents deterministic fixtures, DB outage simulation and safe restarts on a separate instance: its own `<db>_harness` database, its own limited role, its own uvicorn on a free loopback port. It is independent of `qa-server.sh` and its `<db>_qa` database. It refuses non-loopback hosts, refuses to run with `FACTORY_API_KEY` or `QA_EVIDENCE_TOKEN` set, and will not touch a same-named database that lacks its marker. For local setup, set a nonblank dummy `PAYMENTS_GATEWAY_API_KEY` in `.env` or the process environment so the harness can load application settings. Both the harness and `qa-server.sh` supply a separate dummy value to their isolated app processes. Do not use a deployment credential for QA. Output is JSON and never includes passwords or URLs. Run it only locally; CI agents must not execute checkout code.

```sh
uv run python -m tools.qa_harness up [--reset]      # create DB, seed, start app; idempotent
uv run python -m tools.qa_harness status            # base_url, running, outage
uv run python -m tools.qa_harness fixture user carol@example.com --account Checking=5000 [--admin]
uv run python -m tools.qa_harness fixture reset     # truncate and re-seed demo data
uv run python -m tools.qa_harness outage on|off|status
uv run python -m tools.qa_harness restart           # same port and data, new process
uv run python -m tools.qa_harness down              # stop app, drop DB and role; idempotent
```

- Fixture balances are exact integer cents, set through balanced transfers from the funding account. Repeating a command is a no-op; an existing user keeps its password.
- `outage on` disallows connections to the harness DB and terminates sessions, so the app returns 503s. Run `outage off` before `restart` or `fixture`.
- Always run `down` when finished. State lives in `qa-results/.qa-harness.json` (mode 0600, git-ignored).
