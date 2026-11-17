# Tooling

The repository uses uv, Ruff, pytest, and GitHub Actions. Factory tooling adds review, diff-targeted functional QA, and wiki refreshes; none of these workflows deploys the application to production.

## Local tools

`pyproject.toml` declares Python >=3.12, eight runtime dependencies, and pytest/httpx/Ruff as development dependencies. `uv.lock` records dependency resolution; CI installs with `uv sync --locked`. There is no frontend build: Jinja templates and static files are served by the application.

`AGENTS.md` requires asking before dependency additions or downloads. Use [Development workflow](development-workflow.md) for commands and [Testing](testing.md) for the destructive database fixtures.

`.factory/settings.json` enables `droid-control@factory-plugins` from the Factory plugin marketplace. `.factory/droids/security-reviewer.md` defines a read-only reviewer with only Read/Grep/Glob/LS tools: it reports file/line findings and unverified areas, but cannot run checks or edit code.

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

Dependencies are installed with `--locked --no-dev --no-build` using system Python; the project is not installed. The PR application's virtualenv and code are executed only as the separate `qa-app` OS user, with a scrubbed environment. The workflow starts that server before the step holding the Factory key. The application receives a restricted QA database role, not the database administration URL.

The keyed agent reads files and sends HTTP requests; it must not execute checkout Python, uv, the venv, application code, or arbitrary scripts. Its narrowly prescribed server verification/URL commands run with `env -i`. The lifecycle script refuses to run if agent/evidence credentials are present.

`qa-server.sh verify` checks checkout fingerprint, recorded user/exact server command, and the run's HTTP nonce. This is server freshness, not a sandbox attestation or evidence that candidate code is safe. The fingerprint includes tracked diffs, not untracked file contents. See [Testing](testing.md) for database naming and fixture details.

The QA workflow acknowledges a remaining installer tradeoff: its Droid CLI installer is unpinned and unchecksummed, run in a step without secrets. These controls describe the implementation, not a guarantee of complete isolation.

## Functional QA configuration and evidence

`.factory/skills/qa/config.yaml` maps files to API/web skills and personas. Neither app defines a generic `dev_command`; `.factory/skills/qa/scripts/qa-server.sh` owns the local server lifecycle. There are no preview deployments or remote QA targets.

`.factory/skills/qa/SKILL.md` requires diff-relevant flows, negative coverage, and honest BLOCKED/INCONCLUSIVE outcomes. API evidence records redacted requests and responses; browser evidence uses droid-control snapshots/screenshots. Reports go to `qa-results/report.md` using `.factory/skills/qa/REPORT-TEMPLATE.md`. Do not include passwords, JWTs, or CSRF tokens.

Current settings are `video_evidence: false` and `droid_control.compose: false`. Compose rendering is only used when both are true. Evidence uploads are conditional, not a prerequisite for normal text reporting. If browser/capture tooling is missing, record the blocked coverage rather than claiming a browser pass. This documentation run had no capture tools and performed no functional QA.

## Wiki publication boundary

The refresh job restores only `droid-wiki/` from `factory/droid-wiki` when available, reads full history, and runs `/wiki` with read-only repository permissions. Its artifact includes hidden wiki metadata but excludes `video/` and `images/`, and is retained for one day.

Only `wiki-publish` receives contents/PR write permissions. It downloads the artifact and opens or updates `factory/droid-wiki` against the default branch with title `docs: refresh wiki`. Publication failure becomes a warning; generation may already have completed or synced to the hosted wiki when cloud sync is enabled.

`FACTORY_WIKI_PR_TOKEN` is optional; otherwise publication uses `GITHUB_TOKEN`. PRs created with that fallback do not trigger other workflows, so do not assume their checks will run. The workflow documents contents/PR write scopes for the optional token. See [Deployment](../deployment.md) for what is—and is not—deployed.

## Credentials and update automation

Secret names referenced by the agent workflows are `FACTORY_API_KEY`, conditional `QA_EVIDENCE_TOKEN`, and optional `FACTORY_WIKI_PR_TOKEN`; `GITHUB_TOKEN` supplies the built-in publication fallback. Application/server settings use `DATABASE_URL`, `JWT_SECRET`, and `SEED_PASSWORD`. Do not copy actual credential values into documentation or artifacts.

`.github/dependabot.yml` schedules weekly uv and GitHub Actions updates with a seven-day default cooldown. Update PRs still need review and testing; scheduling does not establish dependency freshness or automatic merge.
