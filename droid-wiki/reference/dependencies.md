# Dependencies

LedgerLite declares eight runtime and three development dependencies in `pyproject.toml`. The table records versions inspected in `uv.lock`, not claims about current upstream releases.

## Python packages

Python is declared `>=3.12` in `pyproject.toml`; `.python-version` selects `3.12`. The project uses uv without packaging itself (`tool.uv.package=false`).

| Direct runtime declaration | Locked version | Role |
| --- | --- | --- |
| `fastapi>=0.115` | `0.163.1` | Routing, dependency injection, request/response validation |
| `uvicorn[standard]>=0.30` | `0.54.0` | ASGI server and standard runtime extras |
| `psycopg[binary,pool]>=3.2` | `3.3.9` | Postgres client; binary distribution and pool extras |
| `pydantic-settings>=2.4` | `2.16.3` | Typed environment and dotenv settings |
| `pyjwt>=2.9` | `2.15.1` | HS256 token encoding/decoding |
| `argon2-cffi>=23.1` | `25.1.0` | Password hashing and verification |
| `jinja2>=3.1` | `3.1.6` | Server-rendered HTML templates |
| `python-multipart>=0.0.9` | `0.0.34` | Form request parsing |

The psycopg extras resolve `psycopg-binary==3.3.9` and `psycopg-pool==3.3.3`. They are not additional direct declarations. Pydantic and Starlette are also transitive dependencies, not extra direct entries.

| Direct development declaration | Locked version | Role |
| --- | --- | --- |
| `pytest>=8.3` | `9.2.0` | Regression tests |
| `httpx>=0.27` | `0.28.1` | FastAPI/Starlette test client support |
| `ruff>=0.6` | `0.17.5` | Linting and formatting |

`uv.lock` uses lock format version 1, revision 5, and records resolved packages/artifacts for the supported Python range. Declared lower bounds are not the installed-version record. CI uses `uv sync --locked` in `.github/workflows/ci.yml`, so its dependency sync must agree with the lockfile.

## Browser and database dependencies

`app/static/htmx.min.js` is a vendored htmx **2.0.4** asset, identified by its version property. It is served locally, with no npm build or frontend package manifest. This documentation inspected version metadata without reproducing the minified body.

`docker-compose.yml`, `.github/workflows/ci.yml`, and `.github/workflows/qa.yml` use `postgres:16`. This is a major-version image tag, not a fixed patch version or digest. The application uses Postgres-specific identity columns, UUID generation, row locks, constraints, and PL/pgSQL triggers from `db/schema.sql`.

## Automation dependencies and updates

GitHub Actions workflows use checkout, uv setup, Node setup, artifact upload/download, GitHub Script, Factory's Droid action, and a pull-request creation action. Pinning differs by workflow:

* `.github/workflows/ci.yml` uses `actions/checkout@v7` and `astral-sh/setup-uv@v10.3.3`.
* `.github/workflows/qa.yml` pins checkout, setup-uv, setup-node, artifact upload, and GitHub Script to commit SHAs with version comments.
* `.github/workflows/droid.yml` and `.github/workflows/droid-review.yml` pin `Factory-AI/droid-action` to commit `260eac443df653198ed70e9ea9d32980cfabde23` (commented v13), alongside tagged checkout.
* `.github/workflows/droid-wiki-refresh.yml` uses tagged checkout and artifact actions plus SHA-pinned `peter-evans/create-pull-request`.

`.github/dependabot.yml` schedules weekly updates for the `uv` and `github-actions` ecosystems at repository root, with a seven-day default cooldown. It does not configure Docker image updates or a vendored htmx update mechanism. Configuration alone does not prove dependencies are current, reviewed, or vulnerability-free.

Application boundaries are described in [Architecture](../overview/architecture.md). For automation and contribution requirements, see [Tooling](../how-to-contribute/tooling.md) and [Contributing](../how-to-contribute/index.md). No dependency installation, download, or runtime test was performed for this reference.
