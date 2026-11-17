# Source this after loading credentials (locally: `set -a; . ./.env; set +a`).
# It points DATABASE_URL at the throwaway "<db>_qa" database, so a later .env
# load can never leave the development database selected, and splits the URL into
# the QA_PG_* parts that qa-server.sh uses to build other URLs. Idempotent.
# Pure shell on purpose: nothing from the checked-out project runs here.

case ${DATABASE_URL:-} in
  postgres://* | postgresql://*) ;;
  *) echo "qa-env: DATABASE_URL must be a postgresql:// URL" >&2; return 1 ;;
esac

QA_PG_SCHEME=${DATABASE_URL%%://*}
QA_PG_QUERY=
case $DATABASE_URL in
  *\?*) QA_PG_QUERY="?${DATABASE_URL#*\?}" ;;
esac
_qa_rest=${DATABASE_URL%%\?*}
_qa_rest=${_qa_rest#*://}
_qa_authority=${_qa_rest%%/*}
QA_PG_DB=
case $_qa_rest in
  */*) QA_PG_DB=${_qa_rest#*/} ;;
esac
QA_PG_USERINFO=
QA_PG_HOSTPORT=$_qa_authority
case $_qa_authority in
  *@*)
    QA_PG_USERINFO="${_qa_authority%@*}@"
    QA_PG_HOSTPORT=${_qa_authority##*@}
    ;;
esac
[ -n "$QA_PG_DB" ] || QA_PG_DB=ledgerlite
case $QA_PG_DB in
  *_qa) ;;
  *) QA_PG_DB=${QA_PG_DB}_qa ;;
esac
unset _qa_rest _qa_authority

DATABASE_URL="$QA_PG_SCHEME://$QA_PG_USERINFO$QA_PG_HOSTPORT/$QA_PG_DB$QA_PG_QUERY"
export DATABASE_URL
