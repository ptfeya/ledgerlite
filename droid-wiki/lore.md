# Lore

LedgerLite's available history spans **2026-11-16–2026-11-17: 14 commits**, not a multi-year evolution. A working ledger, API, browser UI and tests arrived together, followed by focused authorization, failure-handling, recipient UX and automation changes. There is no earlier wiki history in this repository snapshot.

Dates below use commit author dates in their recorded timezone, UTC−05:00. The narrative follows `origin/main` through `5cf0fa6`; it does not infer unpublished plans. See [By the numbers](by-the-numbers.md) for reproducible measurements and [Architecture](overview/architecture.md) for the current design.

## Three short eras

### 1. 2026-11-16, 13:16–16:40: from placeholder to working service

- **2026-11-16, 13:16:** [The initial commit](https://github.com/ptfeya/ledgerlite/commit/2e8245c285acc37e5a0c0b818b1c01c282a8c325) contained a one-line `README.md`.
- **2026-11-16, 16:40:** [The application landed together](https://github.com/ptfeya/ledgerlite/commit/e5e4b82549b691b1945c97e15befffec1de2ec92): FastAPI and Postgres, integer-cent double-entry transfers, idempotency, deterministic account locks, overdraft rejection, ownerless funding, bearer-authenticated JSON routes and cookie/CSRF-protected HTML pages. The same commit added schema constraints/triggers, tests, Compose and CI.

This was an initial implementation, not evidence of a rewrite from an earlier payment engine.

### 2. 2026-11-16, 16:54–2026-11-17, 07:05: explicit guardrails and friendlier transfers

- **2026-11-16, 16:54:** [Repository instructions](https://github.com/ptfeya/ledgerlite/commit/922cb057527e4c58a5bc08be19648cd0a4eb9ff2) made money, CSRF, authentication and testing rules explicit in `AGENTS.md`.
- **2026-11-16, 17:24:** [CI/dependency maintenance changed](https://github.com/ptfeya/ledgerlite/commit/b3e9a7275bd4cd1d5e07cd18c1d336c449cf6b06): updated Actions, a pinned Ubuntu runner label and weekly Dependabot configuration.
- **2026-11-16, 18:06:** [Login error handling](https://github.com/ptfeya/ledgerlite/commit/181b3e481841cb64c2e9dd705ce8954729f22657) added generic database/pool-outage responses and inline HTML validation errors, with regression tests.
- **2026-11-16, 23:15:** [Reconciliation became admin-only](https://github.com/ptfeya/ledgerlite/commit/e4734027990123ed2bb5dfdc85fc0aab31eb3e6f). The schema gained a default-false `is_admin` flag and an idempotent upgrade; seeding gained explicit admin selection.
- **2026-11-17, 00:15:** [Factory tag/review workflows arrived](https://github.com/ptfeya/ledgerlite/commit/823700a8a18965a37f13d2728a8f88d21a353208).
- **2026-11-17, 00:51:** [The browser transfer form switched to recipient email](https://github.com/ptfeya/ledgerlite/commit/cc56774ac1190350e54a6eed13ceba744f59b1d2), resolving exactly one non-system Checking account. The account-ID JSON API and central transfer executor stayed in place.
- **2026-11-17, 07:05:** [UI transfer outages gained generic 503 handling](https://github.com/ptfeya/ledgerlite/commit/15d2fa24c0018374304a964752b2f1a8d1abfb36), with retry/idempotency regression coverage for ordinary and htmx submissions.

### 3. 2026-11-17, 09:08–10:27: automated QA, isolation and documentation policy

- **2026-11-17, 09:08:** [Factory QA skills and workflow](https://github.com/ptfeya/ledgerlite/commit/d4c953cfac147f39b6d3ac1dc41f6e692e7c445f) introduced API/web flows and isolated app-server setup.
- **2026-11-17, 09:17:** [Automatic review was extended to new pushes on open PRs](https://github.com/ptfeya/ledgerlite/commit/13ecc3fa83788034db5e7f5d173d0fbdba7cba66).
- **2026-11-17, 09:33:** [QA isolation and verification were hardened](https://github.com/ptfeya/ledgerlite/commit/631884ce9000cfeb0ab0102f33af2f90f30528d0): shell-based verification, a limited QA database role, tighter file access and replacement of PR-provided instruction paths with base-branch copies. The skill explicitly distinguishes server freshness from trust.
- **2026-11-17, 10:18:** [A read-only security-reviewer droid was added and public API documentation routes were disabled](https://github.com/ptfeya/ledgerlite/commit/708e25fc132d696f7f09c985a14bf6d7c89be510).
- **2026-11-17, 10:27:** [The Droid Wiki refresh workflow arrived](https://github.com/ptfeya/ledgerlite/commit/5cf0fa6e5df944dca6aadd15a13c2bcf72a28500). This added generation/rolling-PR automation, not a previously checked-in wiki.

Implementation details and current trust boundaries are in [Tooling](how-to-contribute/tooling.md) and [Security](security.md).

## Longest-surviving features

All features below were introduced on **2026-11-16** in [the application commit](https://github.com/ptfeya/ledgerlite/commit/e5e4b82549b691b1945c97e15befffec1de2ec92) and remain present at the **2026-11-17** snapshot.

“Subsequent changes” counts later commits touching the named implementation path, excluding its creation. It is a reproducible path-history proxy, **not** a claim that each commit changed every feature in that file.

| Surviving capability | Representative implementation | Subsequent changes through 2026-11-17 |
|---|---|---:|
| Double-entry transfer executor, account locks and replay behavior | `app/ledger.py` | 1 |
| Zero-sum and append-only database safeguards | `db/schema.sql` | 1 |
| JWT/password/CSRF primitives | `app/security.py` | 0 |
| JSON login, accounts, transfers, entries and reconciliation routes | `app/routes/api.py` | 2 |
| Server-rendered login, accounts, transfer and history pages | `app/routes/pages.py` | 3 |
| Seeded users and balanced opening funding | `app/seed.py` | 1 |

For example, the **2026-11-17** change to `app/ledger.py` added recipient resolution; it did not replace the transfer executor. The **2026-11-16** schema change added administrator state rather than replacing the ledger tables. The original `tests/test_idempotency.py` also survives with **zero subsequent path changes** through **2026-11-17**. Survival for one day is continuity evidence, not a long-term stability claim.

## What was replaced or restricted

These are concrete behavior changes, not invented retired subsystems:

| Date | Previous behavior | Replacement/current behavior | Evidence |
|---|---|---|---|
| 2026-11-16 | Any authenticated API user could call `/reconcile` | Bearer-authenticated admin dependency; ordinary users receive 403 | [e473402](https://github.com/ptfeya/ledgerlite/commit/e4734027990123ed2bb5dfdc85fc0aab31eb3e6f), `app/auth.py`, `app/routes/api.py` |
| 2026-11-17 | Browser transfer form accepted destination `to_account_id` | Form accepts `to_email`, resolves a Checking account and rejects sending to self; JSON API remains ID-based | [cc56774](https://github.com/ptfeya/ledgerlite/commit/cc56774ac1190350e54a6eed13ceba744f59b1d2), `app/routes/pages.py`, `app/templates/transfer.html`, `app/ledger.py` |
| 2026-11-17 | FastAPI's default documentation/OpenAPI routes were enabled and README directed users to `/docs` | `/docs`, `/redoc` and `/openapi.json` disabled; README documents the API | [708e25f](https://github.com/ptfeya/ledgerlite/commit/708e25fc132d696f7f09c985a14bf6d7c89be510), `app/main.py`, `README.md` |

On **2026-11-16**, login failure handling was improved; on **2026-11-17**, UI transfer failure handling was improved. Those commits refine error responses and tests, rather than deprecating a separate authentication or payments subsystem.

## Rewrites and growth trajectory

**No major application rewrite is evidenced in the available history through 2026-11-17.** The initial working design remains a FastAPI service with Postgres, central ledger functions and Jinja/htmx pages. The **2026-11-17** QA-hardening commit substantially edited shell automation (`.factory/skills/qa/scripts/qa-server.sh`: 92 additions, 72 deletions), but that is not a rewrite of the ledger engine.

The following checkpoints count all tracked files and physical lines, including comments/blanks, lockfile and vendor assets; generated wiki content is excluded:

| Dated checkpoint | Revision | Tracked files | Physical lines | `app/` + `db/` + `tests/` files / lines |
|---|---|---:|---:|---:|
| 2026-11-16, placeholder | `2e8245c` | 1 | 1 | 0 / 0 |
| 2026-11-16, working application | `e5e4b82` | 37 | 3,376 | 29 / 1,892 |
| 2026-11-16, admin reconciliation | `e473402` | 41 | 3,790 | 31 / 2,226 |
| 2026-11-17, UI outage handling | `15d2fa2` | 45 | 4,266 | 33 / 2,626 |
| 2026-11-17, wiki workflow | `5cf0fa6` | 57 | 5,074 | 34 / 2,638 |

From the working application on **2026-11-16** to the snapshot on **2026-11-17**, the repository grew by **20 files / 1,698 lines**; application/schema/test content grew by **five files / 746 lines**. After the **2026-11-17, 07:05** checkpoint, only **12** of the **808** additional net lines were in application/schema/tests; the rest were automation, instructions and related configuration/documentation. This describes the observed shift toward tooling, not a forecast.

## Source pointers

Historical evidence is the linked commits in `https://github.com/ptfeya/ledgerlite` and `git log origin/main`; path-change counts use `git log origin/main -- <path>`. Current anchors: `app/ledger.py`, `app/security.py`, `app/seed.py`, `app/routes/api.py`, `app/routes/pages.py`, `app/main.py`, `db/schema.sql`, `tests/`, `.factory/skills/qa/`, `.github/workflows/`, `README.md` and `AGENTS.md`. For present-day behavior, follow [Transfers](features/transfers.md), [Web interface](features/web-interface.md) and [JSON API](api.md).
