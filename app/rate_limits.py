from psycopg_pool import ConnectionPool

TRANSFER_ATTEMPTS = 20
TRANSFER_WINDOW_SECONDS = 60


def transfer_retry_after(pool: ConnectionPool, *, user_id: int) -> int:
    """Count a UI transfer attempt. Return zero if allowed, otherwise retry seconds.

    A committed, atomic upsert shares the limit across sessions and workers.
    Keep this transaction separate from transfers so failed transfers count too.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO transfer_rate_limits AS limits (user_id) VALUES (%s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "attempts = CASE "
            "WHEN limits.window_started_at <= statement_timestamp() - %s * interval '1 second' "
            "THEN 1 ELSE LEAST(limits.attempts + 1, %s) END, "
            "window_started_at = CASE "
            "WHEN limits.window_started_at <= statement_timestamp() - %s * interval '1 second' "
            "THEN statement_timestamp() ELSE limits.window_started_at END "
            "RETURNING attempts, GREATEST(1, CEIL(EXTRACT(EPOCH FROM "
            "(window_started_at + %s * interval '1 second' - statement_timestamp()))))::int "
            "AS retry_after",
            (
                user_id,
                TRANSFER_WINDOW_SECONDS,
                TRANSFER_ATTEMPTS + 1,
                TRANSFER_WINDOW_SECONDS,
                TRANSFER_WINDOW_SECONDS,
            ),
        ).fetchone()
    return 0 if row["attempts"] <= TRANSFER_ATTEMPTS else row["retry_after"]
