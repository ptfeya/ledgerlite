#!/usr/bin/env bash
# QA application server lifecycle.
#
#   qa-server.sh start   Recreate the throwaway "<db>_qa" database with a limited role,
#                        seed it, and run uvicorn in the FOREGROUND (background it with the
#                        caller's mechanism, e.g. Execute fireAndForget).
#   qa-server.sh verify  Exit 0 only if the server on the recorded port looks like the one
#                        this script started for this checkout. This is a freshness check
#                        (not stale, not another checkout, not another run), not proof that
#                        the application code is trustworthy.
#   qa-server.sh url     Print the base URL after the same check.
#
# verify and url are pure shell + git + curl + ps: they never run Python, the virtualenv, or
# anything else built from the checkout, so a secret-holding caller can use them. They refuse
# to run when FACTORY_API_KEY or QA_EVIDENCE_TOKEN is set; call them as
#   env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/nonexistent qa-server.sh verify
# start runs the project's Python, so it refuses the same way.
#
# start needs DATABASE_URL (a role that can create roles and databases), JWT_SECRET and
# SEED_PASSWORD in the environment, `uv sync` done, and psql on PATH.
# QA_RUN_AS=<user> runs seed and server as that OS user with a scrubbed environment.
set -euo pipefail

fail() { echo "qa-server: $*" >&2; exit 1; }

[ -z "${FACTORY_API_KEY:-}${QA_EVIDENCE_TOKEN:-}" ] \
  || fail "refusing to run with agent credentials in the environment; use env -i (see header)"

cd "$(git rev-parse --show-toplevel)"
ROOT=$PWD
STATE=qa-results/.qa-server
PY=$ROOT/.venv/bin/python
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

sha256() { if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi; }
fingerprint() { { git rev-parse HEAD; git diff HEAD; } | sha256 | cut -d' ' -f1; }
state_get() { sed -n "s/^$1=//p" "$STATE"; }

verify() {
  [ -f "$STATE" ] || fail "no QA server record; start one"
  local root port nonce user fp
  root=$(state_get QA_ROOT)
  port=$(state_get QA_PORT)
  nonce=$(state_get QA_NONCE)
  user=$(state_get QA_USER)
  fp=$(state_get QA_FINGERPRINT)
  case $port in '' | *[!0-9]*) fail "malformed QA server record" ;; esac
  case $nonce in '' | *[!0-9a-f]*) fail "malformed QA server record" ;; esac
  case $user in '' | *[!A-Za-z0-9_.-]*) fail "malformed QA server record" ;; esac
  [ "$root" = "$ROOT" ] || fail "server belongs to another checkout"
  [ "$fp" = "$(fingerprint)" ] || fail "checkout changed since the server started"

  # Exactly one process, owned by the recorded user, with exactly the command line start used.
  local want="$PY -m uvicorn app.main:app --host 127.0.0.1 --port $port --header x-qa-nonce:$nonce"
  local uid pid found=0
  uid=$(id -u "$user") || fail "recorded user $user does not exist"
  for pid in $(pgrep -u "$user" -f "uvicorn app.main:app" || true); do
    [ "$(ps -o uid= -p "$pid" | tr -d ' ')" = "$uid" ] || continue
    [ "$(ps -o args= -p "$pid")" = "$want" ] || continue
    found=$((found + 1))
  done
  [ "$found" -eq 1 ] || fail "expected one recorded server process, found $found"

  local headers
  headers=$(curl -s -m 5 --connect-timeout 2 -D - -o /dev/null "http://127.0.0.1:$port/ui/login") \
    || fail "port $port did not answer within 5s"
  # No grep -q: it exits at the first match and would SIGPIPE the writers under pipefail.
  printf '%s\n' "$headers" | tr -d '\r' | grep -ix "x-qa-nonce: $nonce" >/dev/null \
    || fail "port $port does not answer with this run's nonce"
  QA_VERIFIED_PORT=$port
}

port_free() { ! (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

start() {
  : "${DATABASE_URL:?}" "${JWT_SECRET:?}" "${SEED_PASSWORD:?}"
  command -v psql >/dev/null || fail "psql is required to create the QA database"
  [ -x "$PY" ] || fail "no virtualenv at $PY; run uv sync first"
  # shellcheck source=qa-env.sh
  . "$HERE/qa-env.sh"

  local nonce port role pw app_url i
  nonce=$(openssl rand -hex 16)
  pw=$(openssl rand -hex 24)
  role=${QA_PG_DB}_app

  port=8000
  for i in $(seq 50); do
    port_free "$port" && break
    port=$((20000 + RANDOM % 20000))
    [ "$i" -lt 50 ] || fail "no free port found"
  done

  # The server gets a role that owns only the QA database, never the admin credentials.
  psql "$QA_PG_SCHEME://$QA_PG_USERINFO$QA_PG_HOSTPORT/postgres$QA_PG_QUERY" \
    -X -q -v ON_ERROR_STOP=1 -v role="$role" -v db="$QA_PG_DB" <<SQL
\set pw '$pw'
SELECT format('CREATE ROLE %I LOGIN', :'role')
  WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 20', :'role', :'pw') \gexec
SELECT format('DROP DATABASE IF EXISTS %I WITH (FORCE)', :'db') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
  app_url="$QA_PG_SCHEME://$role:$pw@$QA_PG_HOSTPORT/$QA_PG_DB$QA_PG_QUERY"

  local user=${QA_RUN_AS:-$(id -un)}
  mkdir -p qa-results
  printf 'QA_ROOT=%s\nQA_PORT=%s\nQA_NONCE=%s\nQA_USER=%s\nQA_FINGERPRINT=%s\n' \
    "$ROOT" "$port" "$nonce" "$user" "$(fingerprint)" > "$STATE"

  local run=(env -i PATH="$PATH" HOME=/nonexistent PYTHONDONTWRITEBYTECODE=1
    DATABASE_URL="$app_url" JWT_SECRET="$JWT_SECRET" SEED_PASSWORD="$SEED_PASSWORD")
  [ -z "${QA_RUN_AS:-}" ] || run=(sudo -n -u "$QA_RUN_AS" "${run[@]}")

  "${run[@]}" "$PY" -m app.seed
  "${run[@]}" "$PY" -m app.seed --admin-email qa-admin@example.com
  exec "${run[@]}" "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$port" \
    --header "x-qa-nonce:$nonce"
}

case "${1:-}" in
  start) start ;;
  verify) verify && echo "qa-server: verified http://localhost:$QA_VERIFIED_PORT" ;;
  url) verify && echo "http://localhost:$QA_VERIFIED_PORT" ;;
  *) fail "usage: qa-server.sh start|verify|url" ;;
esac
