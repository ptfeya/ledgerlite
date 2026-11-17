#!/usr/bin/env bash
# QA application server lifecycle.
#
#   qa-server.sh start   Recreate the throwaway "<db>_qa" database, seed it, and run
#                        uvicorn in the FOREGROUND (background it with the caller's
#                        mechanism, e.g. Execute fireAndForget).
#   qa-server.sh verify  Exit 0 only if the server on the recorded port is the one this
#                        script started, from this checkout, against the QA database.
#   qa-server.sh url     Print the verified base URL.
#
# Needs DATABASE_URL, JWT_SECRET and SEED_PASSWORD in the environment and `uv sync` done.
# QA_RUN_AS=<user> runs seed and server as that OS user with a scrubbed environment.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
ROOT=$PWD
STATE=qa-results/.qa-server
PY=$ROOT/.venv/bin/python
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

# shellcheck source=qa-env.sh
. "$HERE/qa-env.sh"

fingerprint() { { git rev-parse HEAD; git diff HEAD; } | sha256sum | cut -d' ' -f1; }

fail() { echo "qa-server: $*" >&2; exit 1; }

verify() {
  [ -f "$STATE" ] || fail "no QA server record; start one"
  # shellcheck disable=SC1090
  . "$STATE"
  [ "$QA_ROOT" = "$ROOT" ] || fail "server belongs to another checkout"
  [ "$QA_FINGERPRINT" = "$(fingerprint)" ] || fail "checkout changed since the server started"
  pgrep -f "$PY -m uvicorn app.main:app --host 127.0.0.1 --port $QA_PORT " >/dev/null \
    || fail "recorded server process is not running"
  curl -s -D - -o /dev/null "http://127.0.0.1:$QA_PORT/ui/login" | tr -d '\r' \
    | grep -qix "x-qa-nonce: $QA_NONCE" || fail "port $QA_PORT does not answer with this run's nonce"
  QA_NONCE=$QA_NONCE QA_DB_NAME=$QA_DB_NAME "$PY" - <<'PY' || fail "database is not this run's QA database"
import os

import psycopg

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    name, comment = conn.execute(
        "SELECT current_database(), shobj_description(oid, 'pg_database') "
        "FROM pg_database WHERE datname = current_database()"
    ).fetchone()
assert name == os.environ["QA_DB_NAME"] and comment == "qa-nonce:" + os.environ["QA_NONCE"]
PY
}

start() {
  : "${JWT_SECRET:?}" "${SEED_PASSWORD:?}"
  local nonce port db
  nonce=$(openssl rand -hex 16)
  port=$("$PY" -c "
import socket
for p in (8000, 0):
    s = socket.socket()
    try:
        s.bind(('127.0.0.1', p)); print(s.getsockname()[1]); break
    except OSError:
        pass
    finally:
        s.close()
")
  db=$(NONCE=$nonce "$PY" - <<'PY'
import os

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

url = os.environ["DATABASE_URL"]
db = conninfo_to_dict(url)["dbname"]
assert db.endswith("_qa"), db
with psycopg.connect(make_conninfo(url, dbname="postgres"), autocommit=True) as admin:
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db)))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    admin.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(db), sql.Literal("qa-nonce:" + os.environ["NONCE"])
        )
    )
print(db)
PY
)
  mkdir -p qa-results
  printf 'QA_ROOT=%s\nQA_PORT=%s\nQA_NONCE=%s\nQA_DB_NAME=%s\nQA_FINGERPRINT=%s\n' \
    "$ROOT" "$port" "$nonce" "$db" "$(fingerprint)" > "$STATE"

  local run=(env -i PATH="$PATH" HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1
    DATABASE_URL="$DATABASE_URL" JWT_SECRET="$JWT_SECRET" SEED_PASSWORD="$SEED_PASSWORD")
  [ -z "${QA_RUN_AS:-}" ] || run=(sudo -n -u "$QA_RUN_AS" "${run[@]}")

  "${run[@]}" "$PY" -m app.seed
  "${run[@]}" "$PY" -m app.seed --admin-email qa-admin@example.com
  exec "${run[@]}" "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" \
    --header "x-qa-nonce:$nonce"
}

case "${1:-}" in
  start) start ;;
  verify) verify && echo "qa-server: verified http://localhost:$QA_PORT" ;;
  url) verify && echo "http://localhost:$QA_PORT" ;;
  *) fail "usage: qa-server.sh start|verify|url" ;;
esac
