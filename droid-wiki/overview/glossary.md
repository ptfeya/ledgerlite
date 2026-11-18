# Glossary

These terms describe the ledger, HTTP interfaces, and test environments. The implementations below define their meaning in LedgerLite.

| Term | Meaning and source |
| --- | --- |
| Integer cents | The unit for all stored and transferred money. UI text is parsed with `Decimal` and converted to cents in `app/routes/pages.py`; transfers use integers in `app/ledger.py`. |
| Debit / credit | A negative / positive `amount_cents` entry. A normal transfer writes one of each with equal magnitude (`app/ledger.py`, `db/schema.sql`). |
| Double-entry ledger | Signed entries that net to zero for a completed transfer. The global sum also starts at zero because seed funding is balanced (`app/ledger.py`, `app/seed.py`). |
| Derived balance | The sum of an account's ledger entries, computed when queried rather than stored (`app/ledger.py`). |
| Transfer | A UUID-identified record with endpoints, amount, initiating user, idempotency key, and request hash (`db/schema.sql`). |
| Ledger entry | One signed amount associated with an account and transfer. Entries are append-only under the update/delete trigger (`db/schema.sql`). |
| System funding account | The ownerless account used for demo opening balances. Application transfers reject it as either endpoint (`app/seed.py`, `app/ledger.py`). |
| Idempotency key | A caller-supplied key unique within a user. A matching retry returns the original successful transfer (`app/ledger.py`, `db/schema.sql`). |
| Request hash | SHA-256 of the source ID, destination ID, and cents, used to detect a key reused for different transfer input (`app/ledger.py`). |
| Replay | A successful transfer returned again without new ledger entries. The API adds `Idempotent-Replay: true` (`app/routes/api.py`). |
| UI transfer attempt | An authenticated, CSRF-valid form submission counted separately from money movement, including invalid fields, business failures, and replays (`app/routes/pages.py`, `app/rate_limits.py`). |
| Transfer fixed window | A per-user 60-second interval starting with the first attempt. Twenty UI attempts are allowed; later attempts return `429` and do not extend the window (`app/rate_limits.py`, `db/schema.sql`). |
| Reconciliation | A read-only global entry-sum check, exposed to admins by `POST /reconcile`. It is not a repair process or a proof that every transfer is correct (`app/routes/api.py`, `app/ledger.py`). |
| Checking recipient | The UI resolves normalized email to exactly one non-system account named `Checking`; the API instead takes account IDs (`app/ledger.py`, `app/routes/pages.py`). |
| Session cookie | `ll_session`, carrying the same kind of signed JWT used for bearer authentication, but accepted only by page authentication (`app/security.py`, `app/auth.py`). |
| Anonymous CSRF token | A signed random value that must match the login form's cookie and submitted field (`app/security.py`, `app/routes/pages.py`). |
| Session-bound CSRF | An HMAC token derived from the session JWT, required for authenticated form POSTs (`app/security.py`, `app/routes/pages.py`). |
| Throwaway test database | The derived `_test` database recreated by `tests/conftest.py`. It is separate from development data but lives on the same configured server. |
| QA freshness | A shell verifier's process, checkout-fingerprint, and nonce checks. It identifies the intended QA server, not whether application code is trustworthy (`.factory/skills/qa/scripts/qa-server.sh`). |
| Browser QA preflight | A trusted CI check of pinned tools, static-page rendering, and QA login rendering; writes `qa-results/preflight.txt` and exits zero even when checks fail (`.factory/skills/qa/scripts/qa-tools.sh`). |
| BLOCKED | Required tooling or the QA server is unusable. Browser-tool failures block web flows, not API flows (`.factory/skills/qa/SKILL.md`). |
| APP_FAIL | A preflight finding about the application after browser tools work, such as a login page returning HTTP 500. Report FAIL, not BLOCKED (`.factory/skills/qa/scripts/qa-tools.sh`, `.factory/skills/qa/SKILL.md`). |
| QA harness | A local fixture and lifecycle tool with its own marked `<base>_harness` database and loopback server, independent of shell QA (`tools/qa_harness.py`). |
| Exact fixture balance | A requested integer-cent balance reached by balanced funding adjustments, not by mutating existing entries (`tools/qa_harness.py`, `app/ledger.py`). |
| Harness outage | Disabled connections and terminated sessions on only the marked harness database; recovery uses `outage off` (`tools/qa_harness.py`). |

Continue with the [data model](../reference/data-models.md) or [architecture](architecture.md).
