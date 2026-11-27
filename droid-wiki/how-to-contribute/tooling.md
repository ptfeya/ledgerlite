# Tooling

The repository uses uv, Ruff, pytest, and GitHub Actions. Factory tooling adds review, diff-targeted functional QA, and wiki refreshes; none of these workflows deploys the application to production.

## Local tools

`pyproject.toml` declares Python >=3.12, eight runtime dependencies, and pytest/httpx/Ruff as development dependencies. `uv.lock` records dependency resolution; CI installs with `uv sync --locked`. There is no frontend build: Jinja templates and static files are served by the application.

`AGENTS.md` requires asking before dependency additions or downloads. Use [Development workflow](development-workflow.md) for commands and [Testing](testing.md) for the destructive database fixtures.

`.factory/settings.json` enables `droid-control@factory-plugins` from a commit-pinned Factory plugin marketplace. `.factory/droids/security-reviewer.md` defines a read-only reviewer with only Read/Grep/Glob/LS tools: it reports file/line findings and unverified areas, but cannot run checks or edit code.

## Local fixture and lifecycle harness

`tools/qa_harness.py` manages a marked `_harness` database, dedicated restricted application role and loopback Uvicorn process for deterministic users/balances, outages and data-preserving restarts. Usage is in `tools/README.md` and [QA harness](../features/qa-harness.md); regression procedures are in [Testing](testing.md).

This is independent of `.factory/skills/qa/scripts/qa-server.sh` and its `_qa` database. It is neither a replacement for shell functional QA nor the startup path for a keyed CI agent. It refuses nonempty `FACTORY_API_KEY` or `QA_EVIDENCE_TOKEN`; keyed agents must not execute the harness, uv or other checkout Python. Use the workflow's prestarted server and prescribed credential-free verification instead.

`qa-results/.qa-harness.json` contains actual generated database/JWT secrets and must never be uploaded or published. Treat `qa-results/qa-harness-server.log` as private diagnostic output. Public JSON includes a local `base_url` and the literal password reference `SEED_PASSWORD`, not the secret value. This documentation update ran no harness or application code and establishes no pytest, Ruff or functional-QA pass.

## Five GitHub workflows

| Workflow source | Trigger and role |
| --- | --- |
| `.github/workflows/ci.yml` — CI | Pushes to `main` and PRs; Postgres 16 service, locked uv install, Ruff lint/format check, pytest. Repository contents are read-only. |
| `.github/workflows/qa.yml` — QA | PRs or manual dispatch; same-repository, non-Dependabot PRs are eligible. Runs informational functional QA, uploads results for 14 days, and creates/updates the PR report comment. |
| `.github/workflows/droid.yml` — Droid Tag | Selected issue, comment, PR, and review events containing `@droid`; runs the Factory action with issue/PR write permissions but read-only contents. |
| `.github/workflows/droid-review.yml` — Droid Auto Review | Non-draft PR open, synchronize, ready-for-review, and reopen events; automatic code and security review, cancelling superseded runs for the PR. |
| `.github/workflows/droid-wiki-refresh.yml` — Droid Wiki Refresh | Pushes to `main`; serial read-only wiki generation followed by a separate write-enabled rolling documentation PR job. |

Workflow definitions do not reveal actual branch-protection settings. QA is explicitly informational unless configured as required elsewhere; its agent step is `continue-on-error`, so inspect the report, not only the job badge.

## QA trust separation

`.github/workflows/qa.yml` checks out the candidate code and separately checks out base-branch instructions. It removes candidate instruction/config directories and instruction files, including nested case-insensitive matches, and restores those from the base. If the base has no QA skill, it emits INCONCLUSIVE instead of following candidate instructions.

Dependencies are installed with `--locked --no-dev --no-build` using system Python; the project is not installed. The PR application's virtualenv and code are executed only as the separate `qa-app` OS user, with a scrubbed environment. The workflow starts that server before the step holding the Factory key. The application receives a restricted QA database role, not the database administration URL. Before launch, the workflow hard-links a temporary dotenv file holding only a dummy `PAYMENTS_GATEWAY_API_KEY` to `.env` (the `ln -T` fails if `.env` already exists, including as a symlink), so seed and Uvicorn start even when the trusted base-branch launcher does not yet forward that setting. QA never receives a real gateway credential.

The keyed agent reads files and sends HTTP requests; it must not execute checkout Python, uv, the venv, application code, or arbitrary scripts. Its narrowly prescribed server verification/URL commands run with `env -i`. The lifecycle script refuses to run if agent/evidence credentials are present.

`qa-server.sh verify` checks checkout fingerprint, recorded user/exact server command, and the run's HTTP nonce. This is server freshness, not a sandbox attestation or evidence that candidate code is safe. The fingerprint includes tracked diffs, not untracked file contents. See [Testing](testing.md) for database naming and fixture details.

The QA workflow acknowledges a remaining installer tradeoff: its Droid CLI installer is unpinned and unchecksummed, run in a step without secrets. These controls describe the implementation, not a guarantee of complete isolation.

## Pinned browser setup and preflight

The browser stack is development/QA tooling, not an application frontend build:

| Component | Pin and source |
| --- | --- |
| agent-browser | `0.43.0` in `.factory/skills/qa/tools/package.json`; `.factory/skills/qa/tools/package-lock.json` records package integrity hashes and Node `>=22.0.0`. |
| Chrome for Testing, linux64 | `157.0.8103.78` in `.factory/skills/qa/scripts/qa-tools.sh`; zip SHA-256 `9223832977a13db3646201b1341c1f9cf08afa3ceae92540116d7b0c062364fb`. |
| droid-control | Script records `1.2.0`; `.factory/settings.json` pins marketplace `Factory-AI/factory-plugins` to `e63e519a736140c86f37be95a529a0c44dc9a96e`. Preflight checks enabled status and pinned commit, not a separate plugin version string. |

`.github/workflows/qa.yml` selects Node 22 and runs the base-branch copy of `.factory/skills/qa/scripts/qa-tools.sh` before the keyed agent, with no agent/evidence secrets. Installation uses `npm ci --ignore-scripts --no-audit --no-fund`, verifies the Chrome archive checksum, verifies the marketplace checkout commit, installs the plugin, and disables marketplace auto-update. Tools live under `$HOME/qa-tools`; the script exports the agent-browser directory through `GITHUB_PATH` and Chrome through `AGENT_BROWSER_EXECUTABLE_PATH`. Each component failure is recorded in `$HOME/qa-tools/install-failures`; installation returns zero so web coverage can be reported BLOCKED.

The workflow attempts `kernel.apparmor_restrict_unprivileged_userns=0` on Ubuntu 24.04 so Chrome can use its sandbox rather than `--no-sandbox`. The sysctl is optional (`|| true`); browser launch must still pass preflight. If the base branch lacks the tooling script, installation is skipped and the workflow writes a BLOCKED preflight file. A base without the QA skill instead gets the existing INCONCLUSIVE report.

After the separate-user QA server starts, preflight checks manifests, recorded installation failures, pinned agent-browser/Chrome versions, and enabled droid-control at the pinned commit. It checks ffmpeg only when video/Compose needs it and ImageMagick only when configured. A scrubbed `qa-preflight` browser session first opens a static data page containing a password field. Failure there means browser tooling is BLOCKED. After that succeeds, it obtains the QA server URL and checks `/ui/login` over HTTP and in the browser; application HTTP/render failures produce `APP_FAIL`.

Preflight writes `qa-results/preflight.txt`, prints it, appends it to the GitHub step summary when available, closes its browser session, and returns zero. `QA_PREFLIGHT: OK` means no tooling blockers, but may coexist with `APP_FAIL`. The agent reads this file for each affected web run; missing/unreadable means BLOCKED. Tool blockers make all affected web flows BLOCKED while API flows continue; application findings mean FAIL. The keyed agent never executes this script, installs/repairs tooling, or falls back to curl for blocked browser flows. Locally, `.factory/skills/qa/SKILL.md` prescribes `agent-browser --version` and `agent-browser doctor --offline --quick` instead. See [Testing](testing.md) for the report contract and stub-executable regressions, and [Debugging](debugging.md) for safe next actions.

## Functional QA configuration and evidence

`.factory/skills/qa/config.yaml` maps files to API/web skills and personas. Neither app defines a generic `dev_command`; `.factory/skills/qa/scripts/qa-server.sh` owns the local server lifecycle. There are no preview deployments or remote QA targets.

`.factory/skills/qa/SKILL.md` requires diff-relevant flows, negative coverage, and honest BLOCKED/INCONCLUSIVE outcomes. API evidence records redacted requests and responses; browser evidence uses droid-control snapshots/screenshots. Reports go to `qa-results/report.md` using `.factory/skills/qa/REPORT-TEMPLATE.md`. Do not include passwords, JWTs, or CSRF tokens.

Current settings are `video_evidence: false` and `droid_control.compose: false`. Compose rendering is only used when both are true. Evidence uploads are conditional, not a prerequisite for normal text reporting. If browser/capture tooling is missing, record the blocked coverage rather than claiming a browser pass. This documentation run performed no functional QA; runtime tool availability is not established here.

## Wiki publication boundary

The refresh job restores only `droid-wiki/` from `factory/droid-wiki` when available, reads full history, and runs `/wiki` with read-only repository permissions. Its artifact includes hidden wiki metadata but excludes `video/` and `images/`, and is retained for one day.

`.github/workflows/droid-wiki-refresh.yml` pins checkout v7.0.1, upload-artifact v7.0.2, and download-artifact v8.0.2 to full commit SHAs. These are the Node 24 action versions; the pins apply to both checkout steps and the artifact handoff. The pull request action is also SHA-pinned at v8.1.1. These pins are specific to this workflow, not a claim about every action in the repository.

Only `wiki-publish` receives contents/PR write permissions. It downloads the artifact and opens or updates `factory/droid-wiki` against the default branch with title `docs: refresh wiki`. Publication failure becomes a warning; generation may already have completed or synced to the hosted wiki when cloud sync is enabled.

`FACTORY_WIKI_PR_TOKEN` is optional; otherwise publication uses `GITHUB_TOKEN`. PRs created with that fallback do not trigger other workflows, so do not assume their checks will run. The workflow documents contents/PR write scopes for the optional token. See [Deployment](../deployment.md) for what is—and is not—deployed.

## Credentials and update automation

Secret names referenced by the agent workflows are `FACTORY_API_KEY`, conditional `QA_EVIDENCE_TOKEN`, and optional `FACTORY_WIKI_PR_TOKEN`; `GITHUB_TOKEN` supplies the built-in publication fallback. Application/server settings use `DATABASE_URL`, `JWT_SECRET`, `SEED_PASSWORD`, and `PAYMENTS_GATEWAY_API_KEY`; QA paths pass only a dummy gateway key, never a repository or workflow secret. Do not copy actual credential values into documentation or artifacts.

`.github/dependabot.yml` schedules weekly uv and GitHub Actions updates with a seven-day default cooldown. Update PRs still need review and testing; scheduling does not establish dependency freshness or automatic merge.
