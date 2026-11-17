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
| Reconciliation | A read-only global entry-sum check, exposed to admins by `POST /reconcile`. It is not a repair process or a proof that every transfer is correct (`app/routes/api.py`, `app/ledger.py`). |
| Checking recipient | The UI resolves normalized email to exactly one non-system account named `Checking`; the API instead takes account IDs (`app/ledger.py`, `app/routes/pages.py`). |
| Session cookie | `ll_session`, carrying the same kind of signed JWT used for bearer authentication, but accepted only by page authentication (`app/security.py`, `app/auth.py`). |
| Anonymous CSRF token | A signed random value that must match the login form's cookie and submitted field (`app/security.py`, `app/routes/pages.py`). |
| Session-bound CSRF | An HMAC token derived from the session JWT, required for authenticated form POSTs (`app/security.py`, `app/routes/pages.py`). |
| Throwaway test database | The derived `_test` database recreated by `tests/conftest.py`. It is separate from development data but lives on the same configured server. |
| QA freshness | A shell verifier's process, checkout-fingerprint, and nonce checks. It identifies the intended QA server, not whether application code is trustworthy (`.factory/skills/qa/scripts/qa-server.sh`). |

Continue with the [data model](../reference/data-models.md) or [architecture](architecture.md).
