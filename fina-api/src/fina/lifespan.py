import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import timedelta

from dishka import AsyncContainer
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from fina.db.session import DATABASE_STATEMENT_TIMEOUT
from fina.domain.clock import Clock
from fina.runtime import RuntimeState
from fina.services.analysis_worker import AnalysisWorker
from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.audio_fetcher import AudioFetcher
from fina.services.client_profile_worker import ClientProfileWorker
from fina.services.discovery_worker import DiscoveryWorker
from fina.services.fetch_worker import FetchWorker
from fina.services.send_order_worker import SendOrderWorker
from fina.services.worker_loop import generate_worker_id

logger = logging.getLogger(__name__)

# Allow a stuck SQL statement to time out and roll back. External processing
# has a longer deadline; shutdown cancels it early and leaves the claim for
# recover_stale(). HTTP drains for at most another 5 seconds in __main__.py.
SHUTDOWN_GRACE_PERIOD = DATABASE_STATEMENT_TIMEOUT + timedelta(seconds=10)


@asynccontextmanager
async def application_lifecycle(
    container: AsyncContainer,
    *,
    audio_fetcher: AudioFetcher | None,
    audio_analyzer: AudioAnalyzer | None,
    max_retry_attempts: int,
    shutdown_grace_period: timedelta = SHUTDOWN_GRACE_PERIOD,
) -> AsyncGenerator[Callable[[], None]]:
    """Manages application runtime state and background workers.

    Supports one or more ASGI servers sharing a container. The caller
    owns the container and must close it after this lifecycle exits,
    including when lifecycle startup or shutdown raises.
    """
    runtime = await container.get(RuntimeState)
    application_clock = await container.get(Clock)
    runtime.started_at = application_clock.now()
    runtime.started = True
    runtime.draining = False
    runtime.readiness_failure = None

    # No external adapter exists yet; each worker only runs once one is
    # supplied. Discovery, fetching, analysis, customer-profile
    # aggregation, and CRM order posting are independent queues (their
    # own claim/retry state), so each gets its own task rather than
    # sharing a loop: a slow or rate-limited call must not stall the
    # others.
    worker_tasks: list[asyncio.Task[None]] = []
    stop_workers = asyncio.Event()

    def begin_shutdown() -> None:
        # The entry point calls this before waiting for HTTP requests to drain.
        # It is also safe to call again when the lifecycle exits.
        if not runtime.draining:
            logger.info("application draining", extra={"worker_count": len(worker_tasks)})
        runtime.draining = True
        runtime.started = False
        stop_workers.set()

    try:
        if audio_fetcher is not None:
            engine = await container.get(AsyncEngine)
            discovery_worker = DiscoveryWorker(
                engine=engine,
                fetcher=audio_fetcher,
                clock=application_clock,
                worker_id=generate_worker_id(),
            )
            worker_tasks.append(
                asyncio.create_task(
                    discovery_worker.run_forever(stop_workers), name="discovery-worker"
                )
            )
            fetch_worker = FetchWorker(
                engine=engine,
                fetcher=audio_fetcher,
                clock=application_clock,
                worker_id=generate_worker_id(),
                max_attempts=max_retry_attempts,
            )
            worker_tasks.append(
                asyncio.create_task(fetch_worker.run_forever(stop_workers), name="fetch-worker")
            )
        if audio_analyzer is not None:
            engine = await container.get(AsyncEngine)
            analysis_worker = AnalysisWorker(
                engine=engine,
                analyzer=audio_analyzer,
                clock=application_clock,
                worker_id=generate_worker_id(),
                max_attempts=max_retry_attempts,
            )
            worker_tasks.append(
                asyncio.create_task(
                    analysis_worker.run_forever(stop_workers), name="analysis-worker"
                )
            )
            client_profile_worker = ClientProfileWorker(
                engine=engine,
                analyzer=audio_analyzer,
                clock=application_clock,
                worker_id=generate_worker_id(),
                max_attempts=max_retry_attempts,
            )
            worker_tasks.append(
                asyncio.create_task(
                    client_profile_worker.run_forever(stop_workers), name="client-profile-worker"
                )
            )
            send_order_worker = SendOrderWorker(
                engine=engine,
                analyzer=audio_analyzer,
                clock=application_clock,
                worker_id=generate_worker_id(),
                max_attempts=max_retry_attempts,
            )
            worker_tasks.append(
                asyncio.create_task(
                    send_order_worker.run_forever(stop_workers), name="send-order-worker"
                )
            )

        if worker_tasks:
            logger.info(
                "background workers started",
                extra={
                    "worker_count": len(worker_tasks),
                    "worker_names": [task.get_name() for task in worker_tasks],
                },
            )
        else:
            # Expected in api mode; stated explicitly so "no jobs are being
            # processed" is never a mystery when reading a replica's logs.
            logger.info("no background workers started; queues are not being processed")

        logger.info("application started", extra={"worker_count": len(worker_tasks)})
        yield begin_shutdown
    finally:
        begin_shutdown()
        if worker_tasks:
            logger.info(
                "waiting for background workers to finish in-flight jobs",
                extra={
                    "worker_count": len(worker_tasks),
                    "grace_period_seconds": shutdown_grace_period.total_seconds(),
                },
            )
            try:
                async with asyncio.timeout(shutdown_grace_period.total_seconds()):
                    await asyncio.gather(*worker_tasks)
                logger.info("background workers stopped cleanly")
            except TimeoutError:
                logger.warning(
                    "workers did not stop within the shutdown grace period; "
                    "abandoning in-flight jobs for recover_stale() to reclaim",
                    extra={
                        "grace_period_seconds": shutdown_grace_period.total_seconds(),
                        "worker_names": [task.get_name() for task in worker_tasks],
                    },
                )
                for task in worker_tasks:
                    task.cancel()
                await asyncio.gather(*worker_tasks, return_exceptions=True)
        logger.info("lifecycle complete")


def build_lifespan(
    container: AsyncContainer,
    *,
    audio_fetcher: AudioFetcher | None,
    audio_analyzer: AudioAnalyzer | None,
    max_retry_attempts: int,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Adapts application_lifecycle to the shape FastAPI's lifespan= wants
    (a callable taking the app instance) for create_app()'s single-app
    case."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        try:
            async with application_lifecycle(
                container,
                audio_fetcher=audio_fetcher,
                audio_analyzer=audio_analyzer,
                max_retry_attempts=max_retry_attempts,
            ):
                yield
        finally:
            await container.close()
            logger.info("shared resources released")

    return lifespan
