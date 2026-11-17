"""Deliver queued webhook events: `uv run python -m app.webhook_worker [--once]`.

A separate process from the API. It polls webhook_outbox for due events and
sends each one, signed, to the current WEBHOOK_URL. ``--once`` makes at most one
attempt for every event due when it starts, then exits. Without it the worker
polls until SIGTERM or Ctrl-C. Configuration is read at startup: restart the
worker after changing WEBHOOK_URL or WEBHOOK_SIGNING_SECRET (the new values
also apply to events that are already queued).

Exit status: 0 on success or when webhooks are disabled, 1 if a delivery
transaction failed in --once mode, 2 for invalid configuration.
"""

import argparse
import logging
import random
import signal
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from app import webhooks
from app.config import Settings, get_settings
from app.db import make_pool

APPLICATION_NAME = "ledgerlite-webhook-worker"
DEFAULT_POLL_INTERVAL = 5.0

log = logging.getLogger("ledgerlite.webhook_worker")


def make_worker_pool(database_url: str) -> ConnectionPool:
    """One connection: the worker handles one event per transaction."""
    return make_pool(database_url, min_size=1, max_size=1, application_name=APPLICATION_NAME)


def _log_result(result: webhooks.DeliveryResult) -> None:
    reason = f"HTTP {result.status_code}" if result.status_code is not None else result.error or "-"
    if result.outcome == "delivered":
        log.info("event %s delivered on attempt %d (%s)", result.event_id, result.attempt, reason)
    elif result.outcome == "retry":
        log.warning(
            "event %s attempt %d failed (%s); retry due at %s",
            result.event_id,
            result.attempt,
            reason,
            webhooks.utc_timestamp(result.next_attempt_at),
        )
    else:
        log.warning(
            "event %s moved to dead letter after attempt %d (%s)",
            result.event_id,
            result.attempt,
            reason,
        )


def run(
    pool: ConnectionPool,
    settings: Settings,
    *,
    once: bool,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    stop: threading.Event,
    clock: Callable[[], datetime] = webhooks.utc_now,
    uniform: Callable[[float, float], float] = random.uniform,
    wait: Callable[[float], object] | None = None,
    timeouts: webhooks.Timeouts = webhooks.DEFAULT_TIMEOUTS,
) -> int:
    wait = wait or stop.wait
    url = settings.webhook_url
    secret = settings.webhook_signing_secret.get_secret_value()
    # --once only considers events due at startup, each at most once, so a
    # zero-delay retry or a steady stream of new transfers cannot keep it running.
    due_at = clock() if once else None
    attempted: list[UUID] = []

    while not stop.is_set():
        try:
            with pool.connection() as conn:
                result = webhooks.deliver_next(
                    conn,
                    url=url,
                    secret=secret,
                    clock=clock,
                    uniform=uniform,
                    due_at=due_at,
                    exclude=attempted,
                    timeouts=timeouts,
                )
        except Exception as exc:
            # Rolled back: the event keeps its previous state. Log only the type,
            # since database errors can quote row data.
            log.error("delivery transaction failed (%s); event left unchanged", type(exc).__name__)
            if once:
                return 1
            wait(poll_interval)
            continue

        if result is None:
            if once:
                return 0
            # The connection is back in the pool before waiting: nothing is held.
            wait(poll_interval)
            continue
        if once:
            attempted.append(result.event_id)
        _log_result(result)
    return 0


def _config_errors(exc: ValidationError) -> str:
    # hide_input_in_errors keeps values out; report only field names and reasons.
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
        for error in exc.errors()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deliver queued LedgerLite webhook events.")
    parser.add_argument(
        "--once", action="store_true", help="attempt each currently due event once, then exit"
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="seconds to wait when no event is due (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        settings = get_settings()
    except ValidationError as exc:
        log.error("invalid configuration: %s", _config_errors(exc))
        return 2
    if not settings.webhooks_enabled:
        log.info("webhooks are disabled (WEBHOOK_URL is empty); nothing to deliver")
        return 0

    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())

    pool = make_worker_pool(settings.database_url)
    pool.open(wait=True, timeout=10)
    try:
        log.info("webhook worker started (%s)", "once" if args.once else "polling")
        return run(pool, settings, once=args.once, poll_interval=args.poll_interval, stop=stop)
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())
