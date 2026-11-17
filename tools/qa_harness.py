"""Isolated fixture and lifecycle harness for QA agents (local use only).

    uv run python -m tools.qa_harness up [--reset]
    uv run python -m tools.qa_harness status
    uv run python -m tools.qa_harness fixture user EMAIL [--admin] [--account NAME=CENTS ...]
    uv run python -m tools.qa_harness fixture reset
    uv run python -m tools.qa_harness outage on|off|status
    uv run python -m tools.qa_harness restart
    uv run python -m tools.qa_harness down

Everything runs against a throwaway "<db>_harness" database owned by its own
limited role, served by its own uvicorn instance on a free loopback port. It never
touches the development ("<db>"), test ("<db>_test") or QA-server ("<db>_qa")
databases. Output is JSON; passwords, tokens and database URLs are never printed.
"""

import argparse
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "qa-results" / ".qa-harness.json"
LOG_PATH = ROOT / "qa-results" / "qa-harness-server.log"

SUFFIX = "_harness"
DB_MARKER = "ledgerlite-qa-harness"
ADMIN_EMAIL = "qa-admin@example.com"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
FORBIDDEN_ENV = ("FACTORY_API_KEY", "QA_EVIDENCE_TOKEN")
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_STRIP_SUFFIXES = ("_harness", "_test", "_qa")


class HarnessError(Exception):
    """A refusal or failure with a message that is safe to show."""


@dataclass(frozen=True)
class Target:
    dbname: str
    role: str
    admin_url: str  # connects to the maintenance database "postgres"
    base_url: str  # same server, no database chosen yet

    def url_for(self, dbname: str, user: str | None = None, password: str | None = None) -> str:
        kwargs: dict = {"dbname": dbname}
        if user is not None:
            kwargs.update(user=user, password=password)
        return make_conninfo(self.base_url, **kwargs)


def resolve_target(database_url: str, env: dict[str, str] | None = None) -> Target:
    """Derive the isolated target from the configured server URL, or refuse."""
    env = os.environ if env is None else env
    present = [name for name in FORBIDDEN_ENV if env.get(name)]
    if present:
        raise HarnessError(f"refusing to run with agent credentials in the environment: {present}")

    info = conninfo_to_dict(database_url)
    host = info.get("host") or ""
    # An empty host or a path means a local unix socket.
    if host and host not in LOOPBACK_HOSTS and not host.startswith("/"):
        raise HarnessError(f"refusing non-local database host {host!r}")
    if info.get("hostaddr") and info["hostaddr"] not in LOOPBACK_HOSTS:
        raise HarnessError("refusing non-local database hostaddr")

    base = info.get("dbname") or "ledgerlite"
    for suffix in _STRIP_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    dbname = f"{base}{SUFFIX}"
    role = f"{base}{SUFFIX}_app"
    for ident in (dbname, role):
        if not _IDENT.match(ident):
            raise HarnessError(f"unsafe database identifier {ident!r}")
    if not dbname.endswith(SUFFIX):
        raise HarnessError("target database must end with _harness")

    base_url = make_conninfo(database_url, dbname="postgres")
    return Target(dbname=dbname, role=role, admin_url=base_url, base_url=base_url)


def parse_account_specs(specs: list[str]) -> list[tuple[str, int]]:
    """Parse NAME=CENTS pairs. Cents must be a non-negative integer."""
    parsed = []
    for spec in specs:
        name, sep, cents = spec.rpartition("=")
        name = name.strip()
        if not sep or not name or not cents.isascii() or not cents.isdecimal():
            raise HarnessError(f"account spec {spec!r} must look like NAME=CENTS (integer cents)")
        parsed.append((name, int(cents)))
    if len({name for name, _ in parsed}) != len(parsed):
        raise HarnessError("duplicate account names")
    return parsed


# --- state and process helpers -------------------------------------------------------------


def load_state() -> dict | None:
    try:
        return json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return None


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle)
    os.replace(tmp, STATE_PATH)


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    except OSError:
        return ""


def server_alive(state: dict | None) -> bool:
    """True only for a live process whose command line carries this run's nonce."""
    if not state or not state.get("pid"):
        return False
    return f"x-qa-harness:{state['nonce']}" in _cmdline(state["pid"])


def stop_server(state: dict | None) -> bool:
    if not server_alive(state):
        return False
    pid = state["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not server_alive(state):
            return True
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while server_alive(state) and time.monotonic() < deadline:
        time.sleep(0.1)
    return True


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def http_status(port: int, path: str = "/ui/login") -> int | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
            return response.status
    except HTTPError as exc:
        return exc.code
    except OSError:
        return None


def start_server(state: dict, settings) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "PYTHONDONTWRITEBYTECODE": "1",
        "DATABASE_URL": state["app_url"],
        "JWT_SECRET": settings.jwt_secret,
        "SEED_PASSWORD": settings.seed_password,
    }
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(state["port"]),
        "--header",
        f"x-qa-harness:{state['nonce']}",
    ]
    with open(LOG_PATH, "ab") as log:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=ROOT,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    state["pid"] = proc.pid
    save_state(state)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise HarnessError(f"server exited during startup; see {LOG_PATH}")
        if http_status(state["port"]) is not None:
            return
        time.sleep(0.2)
    raise HarnessError(f"server did not answer within 30s; see {LOG_PATH}")


# --- database -------------------------------------------------------------------------------


def _admin(target: Target):
    return psycopg.connect(target.admin_url, autocommit=True, row_factory=dict_row)


def _db_state(admin, target: Target) -> dict | None:
    row = admin.execute(
        "SELECT datallowconn, shobj_description(oid, 'pg_database') AS marker "
        "FROM pg_database WHERE datname = %s",
        (target.dbname,),
    ).fetchone()
    return row


def ensure_database(target: Target, password: str, reset: bool) -> None:
    with _admin(target) as admin:
        existing = _db_state(admin, target)
        if existing is not None and existing["marker"] != DB_MARKER:
            raise HarnessError(
                f"database {target.dbname!r} exists but is not a harness database; refusing"
            )
        role = sql.Identifier(target.role)
        if not admin.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (target.role,)
        ).fetchone():
            admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(role))
        admin.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 20"
            ).format(role, sql.Literal(password))
        )
        if existing is not None and reset:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target.dbname))
            )
            existing = None
        if existing is None:
            dbname = sql.Identifier(target.dbname)
            admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(dbname, role))
            admin.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(dbname, sql.Literal(DB_MARKER))
            )
            admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(dbname))
        else:
            admin.execute(
                sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS true").format(
                    sql.Identifier(target.dbname)
                )
            )


def drop_database(target: Target) -> bool:
    with _admin(target) as admin:
        existing = _db_state(admin, target)
        if existing is not None and existing["marker"] != DB_MARKER:
            raise HarnessError(
                f"database {target.dbname!r} exists but is not a harness database; refusing"
            )
        if existing is not None:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(target.dbname))
            )
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(target.role)))
        return existing is not None


def set_outage(target: Target, on: bool) -> None:
    with _admin(target) as admin:
        existing = _db_state(admin, target)
        if existing is None or existing["marker"] != DB_MARKER:
            raise HarnessError("no harness database; run `up` first")
        dbname = sql.Identifier(target.dbname)
        allow = "false" if on else "true"
        admin.execute(sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS " + allow).format(dbname))
        if on:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (target.dbname,),
            )


def outage_active(target: Target) -> bool:
    with _admin(target) as admin:
        existing = _db_state(admin, target)
    return bool(existing and existing["marker"] == DB_MARKER and not existing["datallowconn"])


# --- fixtures -------------------------------------------------------------------------------


def balance_of(conn: psycopg.Connection, account_id: int) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0)::bigint AS b FROM ledger_entries "
        "WHERE account_id = %s",
        (account_id,),
    ).fetchone()["b"]


def _system_account(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE is_system").fetchone()
    if row:
        return row["id"]
    from app.seed import SYSTEM_ACCOUNT_NAME

    return conn.execute(
        "INSERT INTO accounts (name, is_system) VALUES (%s, true) RETURNING id",
        (SYSTEM_ACCOUNT_NAME,),
    ).fetchone()["id"]


def set_balance(conn: psycopg.Connection, account_id: int, cents: int) -> None:
    """Move an account to exactly `cents` through the system funding account.

    Each adjustment is one balanced transfer, so the ledger stays at zero.
    """
    delta = cents - balance_of(conn, account_id)
    if delta == 0:
        return
    system_id = _system_account(conn)
    src, dst = (system_id, account_id) if delta > 0 else (account_id, system_id)
    amount = abs(delta)
    seq = conn.execute(
        "SELECT COUNT(*) AS n FROM transfers WHERE user_id IS NULL AND idempotency_key LIKE %s",
        (f"harness-fund-{account_id}-%",),
    ).fetchone()["n"]
    transfer_id = conn.execute(
        "INSERT INTO transfers (user_id, idempotency_key, request_hash, from_account_id, "
        "to_account_id, amount_cents) VALUES (NULL, %s, 'harness', %s, %s, %s) RETURNING id",
        (f"harness-fund-{account_id}-{seq}", src, dst, amount),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO ledger_entries (transfer_id, account_id, amount_cents) "
        "VALUES (%s, %s, %s), (%s, %s, %s)",
        (transfer_id, src, -amount, transfer_id, dst, amount),
    )


def ensure_user(
    conn: psycopg.Connection,
    email: str,
    password: str,
    accounts: list[tuple[str, int]],
    admin: bool = False,
) -> dict:
    """Create or update a user and set exact account balances. Safe to repeat.

    An existing user keeps its password. Accounts named in `accounts` are created
    if missing and moved to the stated balance.
    """
    from app.security import hash_password

    email = email.strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        raise HarnessError("invalid email")
    row = conn.execute(
        "INSERT INTO users (email, password_hash, is_admin) VALUES (%s, %s, %s) "
        "ON CONFLICT (email) DO UPDATE SET is_admin = users.is_admin OR EXCLUDED.is_admin "
        "RETURNING id",
        (email, hash_password(password), admin),
    ).fetchone()
    user_id = row["id"]
    result = {}
    for name, cents in accounts:
        found = conn.execute(
            "SELECT id FROM accounts WHERE user_id = %s AND name = %s ORDER BY id LIMIT 1",
            (user_id, name),
        ).fetchone()
        account_id = (
            found["id"]
            if found
            else conn.execute(
                "INSERT INTO accounts (user_id, name) VALUES (%s, %s) RETURNING id",
                (user_id, name),
            ).fetchone()["id"]
        )
        set_balance(conn, account_id, cents)
        result[name] = {"account_id": account_id, "balance_cents": cents}
    conn.commit()
    return {"email": email, "user_id": user_id, "accounts": result}


def reset_data(conn: psycopg.Connection, password: str) -> None:
    from app.seed import seed, seed_admin

    conn.execute(
        "TRUNCATE ledger_entries, transfers, transfer_rate_limits, accounts, users "
        "RESTART IDENTITY CASCADE"
    )
    seed(conn, password)
    seed_admin(conn, ADMIN_EMAIL, password)
    conn.commit()


def _app_conn(state: dict) -> psycopg.Connection:
    return psycopg.connect(state["app_url"], row_factory=dict_row)


# --- commands -------------------------------------------------------------------------------


def _settings():
    from app.config import Settings

    return Settings()


def _public(state: dict, target: Target) -> dict:
    return {
        "running": server_alive(state),
        "base_url": f"http://localhost:{state['port']}",
        "database": target.dbname,
        "outage": outage_active(target),
        "personas": ["alice@example.com", "bob@example.com", ADMIN_EMAIL],
        "password": "SEED_PASSWORD",
    }


def cmd_up(target: Target, settings, reset: bool) -> dict:
    state = load_state()
    if state and server_alive(state) and not reset:
        return {**_public(state, target), "changed": False}
    stop_server(state)
    password = secrets.token_hex(24)
    ensure_database(target, password, reset)
    old_port = (state or {}).get("port")
    state = {
        "dbname": target.dbname,
        "port": old_port if old_port and _port_free(old_port) else free_port(),
        "nonce": secrets.token_hex(16),
        "app_url": target.url_for(target.dbname, target.role, password),
        "pid": None,
    }
    save_state(state)
    from app.db import apply_schema

    with _app_conn(state) as conn:
        apply_schema(conn)
        from app.seed import seed, seed_admin

        seed(conn, settings.seed_password)
        seed_admin(conn, ADMIN_EMAIL, settings.seed_password)
        conn.commit()
    start_server(state, settings)
    return {**_public(state, target), "changed": True}


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _require_state() -> dict:
    state = load_state()
    if not state:
        raise HarnessError("harness is not up; run `up`")
    return state


def cmd_restart(target: Target, settings) -> dict:
    state = _require_state()
    stop_server(state)
    if outage_active(target):
        raise HarnessError("database outage is active; run `outage off` before restart")
    state["nonce"] = secrets.token_hex(16)
    state["pid"] = None
    if not _port_free(state["port"]):
        raise HarnessError(f"port {state['port']} is in use by another process")
    start_server(state, settings)
    return {**_public(state, target), "changed": True}


def cmd_down(target: Target) -> dict:
    state = load_state()
    stopped = stop_server(state)
    dropped = drop_database(target)
    STATE_PATH.unlink(missing_ok=True)
    LOG_PATH.unlink(missing_ok=True)
    return {"server_stopped": stopped, "database_dropped": dropped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qa_harness", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up", help="create the isolated database and start the app")
    up.add_argument("--reset", action="store_true", help="recreate the database from scratch")
    sub.add_parser("status")
    sub.add_parser("restart", help="restart the app, keeping data")
    sub.add_parser("down", help="stop the app and drop the database and role")
    outage = sub.add_parser("outage")
    outage.add_argument("mode", choices=["on", "off", "status"])
    fixture = sub.add_parser("fixture")
    fsub = fixture.add_subparsers(dest="kind", required=True)
    user = fsub.add_parser("user")
    user.add_argument("email")
    user.add_argument("--admin", action="store_true")
    user.add_argument("--account", action="append", default=[], metavar="NAME=CENTS")
    fsub.add_parser("reset", help="truncate and re-seed the standard demo data")
    return parser


def run(args: argparse.Namespace) -> dict:
    settings = _settings()
    target = resolve_target(settings.database_url)
    cmd = args.command
    if cmd == "up":
        return cmd_up(target, settings, args.reset)
    if cmd == "down":
        return cmd_down(target)
    if cmd == "status":
        state = load_state()
        return _public(state, target) if state else {"running": False}
    if cmd == "restart":
        return cmd_restart(target, settings)
    if cmd == "outage":
        if args.mode != "status":
            _require_state()
            set_outage(target, args.mode == "on")
        return {"outage": outage_active(target)}
    state = _require_state()
    if outage_active(target):
        raise HarnessError("database outage is active; run `outage off` first")
    with _app_conn(state) as conn:
        if args.kind == "reset":
            reset_data(conn, settings.seed_password)
            return {"reset": True}
        accounts = parse_account_specs(args.account)
        return ensure_user(conn, args.email, settings.seed_password, accounts, args.admin)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except HarnessError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except psycopg.OperationalError:
        print(json.dumps({"error": "cannot reach Postgres (is Compose up?)"}), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
