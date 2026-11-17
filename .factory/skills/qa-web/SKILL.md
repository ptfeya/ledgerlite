---
name: qa-web
description: >
  QA tests for the LedgerLite HTML UI under /ui (cookie session, CSRF, htmx): login, logout,
  accounts, transfer form, recipient email lookup, and history. Driven through droid-control.
---

# QA: LedgerLite web UI

Describes WHAT to verify. The orchestrator picks only flows relevant to the diff.

## Droid Control

Invoke the `droid-control` skill before interacting with the app. Use its web route (agent-browser) with Capture and Verify. Load Compose only when both `video_evidence` and `droid_control.compose` are true in `.factory/skills/qa/config.yaml`. Droid-control owns driver, capture, and verification mechanics.

## Testing Target

No preview deployments exist. Test the branch locally:

1. `SEED_PASSWORD` must be available for logging in (CI provides it). Locally, never print `.env` or its values.
2. Follow the orchestrator's Step 4 exactly: check the server with `env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent .factory/skills/qa/scripts/qa-server.sh verify` and reuse it only if it passes. In CI never start a server or execute anything from the checkout. Locally, start one as Step 4 describes, using the Execute tool's `fireAndForget: true` (a shell `&` is killed when the call returns). It uses the throwaway `<db>_qa` database.
3. Base URL: the output of `env -i ... qa-server.sh url` plus `/ui` (normally `http://localhost:8000/ui`).

In CI, read `qa-results/preflight.txt` before any browser step; if it starts with `QA_PREFLIGHT: BLOCKED`, report all web tests BLOCKED with its reasons (qa skill Step 4).

Never fall back to a remote environment. If the server cannot start, report all web tests BLOCKED.

## Authentication

Log in through the form with `alice@example.com` or `bob@example.com`; password is the `SEED_PASSWORD` env var. Do not print it. Do not use the API for UI sessions.

## Flows (menu)

- **ui-login**: valid login lands on accounts; wrong password shows an inline error and stays on login; empty fields show a validation error, not a 500.
- **ui-logout**: logout (a CSRF-protected POST) ends the session; `/ui/accounts` then redirects to login.
- **ui-accounts**: accounts page shows the user's accounts with formatted balances matching the API cents values. The funding account never appears.
- **ui-transfer-happy**: submit a valid amount to another user's recipient email (sending to yourself is rejected); the result partial confirms it, and balances change by exactly that amount.
- **ui-recipient-email**: entering a recipient email (e.g. bob@example.com) resolves to that user's Checking account. Unknown email shows a clear error.
- **ui-transfer-validation**: zero, negative, non-numeric, more than two decimals, and huge amounts show errors; overdraft shows an insufficient-funds error; balances unchanged.
- **ui-double-submit**: submitting the same form twice must not move money twice.
- **ui-history**: history lists the new transfer with correct direction and amount.
- **ui-csrf**: a form POST without a valid CSRF token is rejected (login, logout, and transfer). Test it with forged requests, not by inspecting the page. Use curl with a cookie jar: (a) `GET /ui/login` for the cookie, then `POST /ui/login` with valid credentials but NO csrf field; (b) repeat with a made-up csrf value. Both must return 4xx and create no session. Then log in properly (with the real token), and send `POST /ui/transfer` and `POST /ui/logout` with that session cookie but NO csrf field, then with a made-up one. Each must return 4xx, balances must be unchanged, and the session must still work afterward. Never print the password or real tokens.
- **ui-unauthenticated**: `/ui/accounts`, `/ui/transfer`, `/ui/history` redirect to login without a session.

## Per-persona variations

Repeat transfer and history as alice and bob; each must see only their own accounts and history. After alice sends to bob, bob's history shows the receipt.

## Known Failure Modes

1. **htmx partial not swapped yet.** Wait for the result element to change before snapshotting; each snapshot must differ.
2. **Seed password mismatch.** Login error for seeded users means `SEED_PASSWORD` differs from the value used at seed time.
3. **Browser tooling missing or broken in CI.** Shows up as `QA_PREFLIGHT: BLOCKED` (for example Chrome cannot launch or a pin failed to install). It is an environment problem, not an app failure. Report BLOCKED; never mark the app FAIL for it.
4. **Stale balances.** Assert deltas, not absolute numbers, or restart with `qa-server.sh start`, which recreates the throwaway database (re-seeding alone does not reset it).
