# LedgerLite

LedgerLite is a small payments service with a JSON API and a browser interface. It stores transfers in a double-entry Postgres ledger: balances come from signed ledger entries, not mutable balance columns (`README.md`, `app/ledger.py`, `db/schema.sql`).

## Start here

| Task | Page |
| --- | --- |
| Run the demo locally | [Getting started](getting-started.md) |
| Understand request and database boundaries | [Architecture](architecture.md) |
| Look up ledger and authentication terms | [Glossary](glossary.md) |
| Change money movement safely | [Transfers](../features/transfers.md) |
| Work on browser forms | [Web interface](../features/web-interface.md) |
| Integrate an HTTP client | [JSON API](../api.md) |
| Run regression tests | [Testing](../how-to-contribute/testing.md) |
| Find settings and tables | [Reference](../reference/index.md) |

## What runs

One FastAPI application serves unprefixed JSON endpoints, HTML pages under `/ui`, and local static assets. Both route layers use the same money-movement module. Postgres 16 is the only required external service; Docker Compose starts the database, not the Python application (`app/main.py`, `app/routes/api.py`, `app/routes/pages.py`, `docker-compose.yml`).

The demo seeds Alice and Bob with Checking and Savings accounts. A separate, ownerless funding account supplies their opening balances. Admin access is explicit, and its only API capability is a read-only reconciliation check (`app/seed.py`, `app/routes/api.py`).

## Boundaries

The JSON API authenticates bearer tokens only. The browser uses a session cookie and CSRF-protected forms. The repository does not include registration, login rate limiting, background jobs, a production application image, or a production deployment pipeline (`app/auth.py`, `app/routes/api.py`, `app/routes/pages.py`, `docker-compose.yml`, `.github/workflows/ci.yml`).

This wiki describes the checked-out source, not a certification of production readiness. Begin with the [feature map](../features/index.md) or the [contribution guide](../how-to-contribute/index.md).
