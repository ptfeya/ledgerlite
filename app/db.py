from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def make_pool(
    database_url: str,
    *,
    max_size: int = 10,
    application_name: str | None = None,
) -> ConnectionPool:
    kwargs: dict = {"row_factory": dict_row}
    if application_name:
        kwargs["application_name"] = application_name
    return ConnectionPool(
        database_url,
        min_size=1,
        max_size=max_size,
        kwargs=kwargs,
        open=False,
    )


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text())
    conn.commit()
