---
name: qa
description: >
  Run QA tests for LedgerLite. Analyzes git diff to determine affected areas,
  runs configured test flows with multiple personas, and generates diff-targeted tests.
  Uses droid-control for browser interactions and curl for the JSON API.
  Use when testing PRs, releases, or smoke testing the app.
---

# QA Orchestrator

**SCOPE: manual/functional QA only. Interact with the running app as a real user or API client. Do NOT run or report on lint, ruff, pytest, or any static analysis. Those are handled by CI.**

## Step 1: Load Configuration

Read `.factory/skills/qa/config.yaml` for environment, personas, and app definitions.

For the affected interactive app (`web`), invoke the `droid-control` skill before interacting with it and follow its routing tables:

- Use its web route (agent-browser) with the Capture and Verify stages on every run.
- Load Compose only when both `video_evidence: true` and `droid_control.compose: true`.
- When `droid_control.compose` is false, never invoke Compose. Keep raw captures and text snapshots as evidence.

Read `droid_control.compose` and `video_evidence` on every run. Do not cache them.

## Step 2: Target

Use `default_target` unless the user names another. LedgerLite has no preview deployments: test the checked-out branch by starting it locally (see `qa-api` / `qa-web` sub-skills). Never test against any remote deployment. Respect environment restrictions.

## Step 3: Analyze Git Diff

Diff against an explicit base. In CI use the base given in the prompt (`git diff <base>...HEAD`). Locally use the merge base with the default branch: `git diff $(git merge-base HEAD origin/main)` (fall back to `main`), which covers committed, staged and unstaged changes, plus `git ls-files --others --exclude-standard` for new untracked files. Map changed files to apps using `path_patterns` in config.yaml.

- Runtime dependency changes (`pyproject.toml`, `uv.lock`) are matched to both apps.
- Files matching no app (`.factory/**`, `docs/**`, `.github/**`, `tests/**`, README) belong to no app. Do not run flows for them.
- Run only affected apps' sub-skills. Do not load or pre-flight unaffected apps.
- If no app is affected, report INCONCLUSIVE: "No app code changed -- QA not applicable for this diff."

## Step 4: Pre-flight (affected apps only)

- Check the server with `env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent .factory/skills/qa/scripts/qa-server.sh verify`. Always use `env -i` so no credentials reach the script; it refuses to run if `FACTORY_API_KEY` is set. `verify` is pure shell, git and curl. It never runs Python or anything else from the checkout.
- `verify` is a freshness check, not a trust check. A pass means: a server process owned by the recorded user, with exactly the command line `start` used, is running; the checkout is the same commit and diff it was started from; and the port answers with this run's nonce in `x-qa-nonce`. It does not show that the application code is safe or correct. Treat the server as untrusted code under test. Send it only the seeded QA credentials.
- **CI:** the server was started before you, as a separate OS user, in a step with no secrets. If `verify` fails, report BLOCKED with its message. Never start a server, and never execute anything from the checkout (`python`, `.venv`, `uv`, app code, other scripts). Read files and talk to the server over HTTP only.
- **Local:** if `verify` fails, do not reuse whatever answers on port 8000. Start a fresh server with the Execute tool's `fireAndForget: true`: `env -u FACTORY_API_KEY -u QA_EVIDENCE_TOKEN bash -c 'set -a; . ./.env; set +a; exec .factory/skills/qa/scripts/qa-server.sh start'` (needs Postgres from `docker compose up -d --wait`, `uv sync` done, and `psql` on PATH). It recreates the throwaway `<db>_qa` database, creates a limited role that owns only that database, seeds it including `qa-admin@example.com`, and runs uvicorn in the foreground. The app gets the limited role's URL, never the admin credentials. Poll `verify` until it passes. Never print `.env`.
- Get the base URL from `env -i ... qa-server.sh url`. Start at most one server for all affected apps.
- `droid-control@factory-plugins` active for `web`.

If a pre-flight check fails, report BLOCKED with the error and remediation, then continue with other affected apps.

## Step 5: Execute Diff-Relevant Flows Only

For each affected app read the sub-skill named by `apps.<app>.skill` in `config.yaml` (`.factory/skills/<skill>/SKILL.md`). Its flows are a menu, not a checklist.

1. Pick flows relevant to the diff plus adjacent integration flows.
2. Skip unrelated flows.
3. If no flow covers the change, write a new ad-hoc test for the changed behavior.
4. Never run pytest, ruff, or other automated suites.

Money-safety invariants to keep in mind for every ledger-touching change: transfers net to zero, the funding account is never a transfer endpoint, amounts are integer cents, API rejects session cookies, every form POST needs CSRF.

## Step 6: Evidence Capture

Text snapshots are the primary evidence.

- Web: capture accessibility-tree snapshots through the droid-control web route. Save screenshots to `./qa-results/$RUN_ID/`. Do NOT embed `![image](url)` for artifact files; name the file and say it is in the downloadable artifacts.
- API: record the exact curl command (token redacted) and the status plus trimmed JSON body.
- Each snapshot must show something different. Label what it shows and why it matters.
- When `video_evidence: true`: record one video per web flow with droid-control Capture. If `droid_control.compose` is true, render each with Compose using a `single` layout and the literal `"preset": "factory"`, max 60 seconds, then verify. Upload via the GitHub user-attachments endpoint with `QA_EVIDENCE_TOKEN` and `REPO_ID`, put the returned `url` alone on its own line (never wrapped in `![]()`). If any capture or upload fails, fall back to text evidence and note the file is in artifacts. Never embed a URL you did not get from the upload response.
- Never put passwords, JWTs, or CSRF tokens in evidence.

## Step 7: Test Quality Gate

1. At least half the tests directly verify the changed behavior.
2. Integration checks for the change are valid.
3. No unrelated flows.
4. Include at least one negative test (bad input, wrong persona, missing CSRF, insufficient funds).
5. If you cannot say what the diff changes, mark INCONCLUSIVE, not PASS.

## Step 8: Handle Failures

**Never silently skip a flow. If a flow cannot complete, report it as BLOCKED with what was tried and how the user can fix it.** Then continue with the next flow.

## Step 9: Generate Report

Write `./qa-results/report.md` following `.factory/skills/qa/REPORT-TEMPLATE.md`.

- Start with `## QA Report` and the results table.
- Result column uses :white_check_mark: PASS, :x: FAIL, :no_entry: BLOCKED, :warning: FLAKY, :grey_question: INCONCLUSIVE.
- Be concise: table, short "Action Required" if any, one collapsed evidence `<details>`.
- No "Behavioral Change Summary", no info table. Do not list setup steps (seed, server start) as test rows.

## Step 10: Suggest Skill Updates

`failure_learning` is `suggest_in_report`: if a BLOCKED or FAIL revealed a new testing-environment insight not already in Known Failure Modes, append this section. Skip bad selectors and expected PR changes. Omit the section if nothing new.

## Suggested Skill Updates (N issues found)

| #   | Severity        | File     | Issue               | Fix Prompt                                                                           |
| --- | --------------- | -------- | ------------------- | ------------------------------------------------------------------------------------ |
| 1   | <emoji> <level> | `<file>` | <short description> | <details><summary>Copy</summary><br>`<full droid prompt to fix the issue>`</details> |

Severities: 🔴 Breaking (fails every run), 🟡 Degraded (intermittent), 🔵 Info (new knowledge). Do not write `skill-updates.json`.
