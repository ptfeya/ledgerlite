# Reference

Use these source-derived tables when you need an exact setting, persistence rule, or declared dependency.

| Page | Covers | Primary sources |
| --- | --- | --- |
| [Configuration](configuration.md) | Application settings, Compose variables, test, shell-QA, and harness derivation | `app/config.py`, `.env.example`, `docker-compose.yml`, `tests/conftest.py`, `.factory/skills/qa/scripts/qa-env.sh`, `tools/qa_harness.py` |
| [Data model](data-models.md) | Five tables, indexes, trigger boundaries, and seed/fixture lifecycle | `db/schema.sql`, `app/seed.py`, `app/ledger.py`, `tools/qa_harness.py` |
| [Dependencies](dependencies.md) | Eight runtime and three development packages, lockfile versions, browser and infrastructure dependencies | `pyproject.toml`, `uv.lock`, `app/static/htmx.min.js`, `.github/dependabot.yml` |

For behavior rather than definitions, see [JSON API](../api.md), [Transfers](../features/transfers.md), [QA harness](../features/qa-harness.md), and [Architecture](../overview/architecture.md).
