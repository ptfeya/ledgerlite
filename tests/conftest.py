import os

# Must be set before the app reads settings. Real env vars win over .env.
os.environ.setdefault("JWT_SECRET", "test-only-secret-not-for-production")
# The suite does not need a real gateway credential, even if the caller has one.
for name in tuple(os.environ):
    if name.upper() == "PAYMENTS_GATEWAY_API_KEY":
        del os.environ[name]
os.environ["PAYMENTS_GATEWAY_API_KEY"] = "test-only-gateway-key"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db import apply_schema, make_pool  # noqa: E402
from app.seed import seed, seed_admin  # noqa: E402
from tests.helpers import TEST_PASSWORD, api_login  # noqa: E402

# Tests run against a throwaway "<db>_test" database on the same server, never
# against the development database.
_base_url = Settings().database_url
_base_dbname = conninfo_to_dict(_base_url).get("dbname") or "ledgerlite"
TEST_DBNAME = _base_dbname if _base_dbname.endswith("_test") else f"{_base_dbname}_test"
TEST_DATABASE_URL = make_conninfo(_base_url, dbname=TEST_DBNAME)
ADMIN_DATABASE_URL = make_conninfo(_base_url, dbname="postgres")

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
get_settings.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _test_database():
    with psycopg.connect(ADMIN_DATABASE_URL, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DBNAME))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DBNAME)))
    with psycopg.connect(TEST_DATABASE_URL) as conn:
        apply_schema(conn)


@pytest.fixture(autouse=True)
def _fresh_data(_test_database):
    with psycopg.connect(TEST_DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("TRUNCATE ledger_entries, transfers, accounts, users RESTART IDENTITY CASCADE")
        seed(conn, TEST_PASSWORD)
        seed_admin(conn, "admin@example.com", TEST_PASSWORD)


@pytest.fixture
def db():
    """Autocommit connection for assertions and direct setup."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
        yield conn


@pytest.fixture
def pool():
    pool = make_pool(TEST_DATABASE_URL)
    pool.open(wait=True, timeout=10)
    yield pool
    pool.close()


@pytest.fixture
def client():
    from app.main import app

    # "localhost" so cookies are not marked Secure and are sent over plain http.
    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


@pytest.fixture
def ids(db):
    """Account ids by owner and name, plus the system account."""
    rows = db.execute(
        "SELECT a.id, a.name, a.is_system, u.email FROM accounts a "
        "LEFT JOIN users u ON u.id = a.user_id"
    ).fetchall()
    result: dict = {"alice": {}, "bob": {}}
    for row in rows:
        if row["is_system"]:
            result["system"] = row["id"]
        else:
            result[row["email"].split("@")[0]][row["name"]] = row["id"]
    return result


@pytest.fixture
def alice(client):
    return api_login(client, "alice")


@pytest.fixture
def bob(client):
    return api_login(client, "bob")


@pytest.fixture
def admin(client):
    return api_login(client, "admin")
