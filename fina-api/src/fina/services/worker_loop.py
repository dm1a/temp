import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from uuid import uuid4

logger = logging.getLogger(__name__)

# Bound the whole job after claiming, including provider retries and database
# work. Leave ample room before another replica's stale recovery (10m minimum).
DEFAULT_PROCESSING_TIMEOUT = timedelta(minutes=5)


def generate_worker_id() -> str:
    """A worker_id unique per process, stable for that process's lifetime."""

    return f"{socket.gethostname()}-{uuid4().hex[:8]}"


async def run_polling_loop(
    stop_event: asyncio.Event,
    *,
    iteration: Callable[[], Awaitable[bool]],
    poll_interval: timedelta,
    worker_id: str,
    worker_kind: str,
) -> None:
    """Repeat `iteration` until `stop_event` is set.

    An exception from a single iteration is logged and treated as "nothing
    processed" rather than stopping the loop, so one bad job or a transient
    database error doesn't take the whole worker down. Shared by every worker
    loop so a future fix to this mechanics only needs to happen once.
    """

    context = {"worker_id": worker_id, "worker_kind": worker_kind}
    logger.info("worker started", extra=context)
    try:
        while not stop_event.is_set():
            try:
                processed = await iteration()
            except Exception:
                logger.exception("worker iteration failed", extra=context)
                processed = False
            if not processed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_interval.total_seconds())
    finally:
        logger.info("worker stopped", extra=context)
