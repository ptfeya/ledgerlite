from psycopg_pool import ConnectionPool

TRANSFER_ATTEMPT_LIMIT = 20
TRANSFER_WINDOW_SECONDS = 60


def transfer_retry_after(pool: ConnectionPool, user_id: int) -> int:
    """Count a UI submission, returning zero if allowed or seconds until retry.

    The atomic upsert shares one fixed window across sessions and app workers.
    Its transaction commits before recipient lookup or money movement, so failed
    transfers consume the same quota as successful ones.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO transfer_rate_limits AS limits (user_id, window_started_at, attempts) "
            "VALUES (%s, statement_timestamp(), 1) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "attempts = CASE "
            "WHEN limits.window_started_at <= statement_timestamp() - %s * INTERVAL '1 second' "
            "THEN 1 ELSE LEAST(limits.attempts + 1, %s) END, "
            "window_started_at = CASE "
            "WHEN limits.window_started_at <= statement_timestamp() - %s * INTERVAL '1 second' "
            "THEN statement_timestamp() ELSE limits.window_started_at END "
            "RETURNING attempts, CEIL(EXTRACT(EPOCH FROM "
            "(window_started_at + %s * INTERVAL '1 second' - clock_timestamp())))::integer "
            "AS retry_after",
            (
                user_id,
                TRANSFER_WINDOW_SECONDS,
                TRANSFER_ATTEMPT_LIMIT + 1,
                TRANSFER_WINDOW_SECONDS,
                TRANSFER_WINDOW_SECONDS,
            ),
        ).fetchone()
    return 0 if row["attempts"] <= TRANSFER_ATTEMPT_LIMIT else max(1, row["retry_after"])
