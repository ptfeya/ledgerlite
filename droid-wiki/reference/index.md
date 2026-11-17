# Reference

Use these source-derived tables when you need an exact setting, persistence rule, or declared dependency.

| Page | Covers | Primary sources |
| --- | --- | --- |
| [Configuration](configuration.md) | Application settings, Compose variables, test and QA derivation | `app/config.py`, `.env.example`, `docker-compose.yml`, `tests/conftest.py`, `.factory/skills/qa/scripts/qa-env.sh` |
| [Data model](data-models.md) | Four tables, indexes, trigger boundaries, and seed lifecycle | `db/schema.sql`, `app/seed.py`, `app/ledger.py` |
| [Dependencies](dependencies.md) | Eight runtime and three development packages, lockfile versions, browser and infrastructure dependencies | `pyproject.toml`, `uv.lock`, `app/static/htmx.min.js`, `.github/dependabot.yml` |

For behavior rather than definitions, see [JSON API](../api.md), [Transfers](../features/transfers.md), and [Architecture](../overview/architecture.md).
