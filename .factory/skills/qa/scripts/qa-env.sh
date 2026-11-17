# Source this after loading credentials (locally: `set -a; . ./.env; set +a`).
# It points DATABASE_URL at the throwaway "<db>_qa" database, so a later .env
# load can never leave the development database selected. Idempotent.
QA_DATABASE_URL=$(.venv/bin/python - <<'PY'
import os

from psycopg.conninfo import conninfo_to_dict, make_conninfo

url = os.environ["DATABASE_URL"]
name = conninfo_to_dict(url).get("dbname") or "ledgerlite"
print(make_conninfo(url, dbname=name if name.endswith("_qa") else f"{name}_qa"))
PY
)
export DATABASE_URL="$QA_DATABASE_URL"
unset QA_DATABASE_URL
