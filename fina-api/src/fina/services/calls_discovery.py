import logging
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from fina.config import Settings
from fina.domain.clock import Clock
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.runtime import RuntimeState

logger = logging.getLogger(__name__)


class CallsDiscoveryScheduler(Protocol):
    async def schedule(self) -> bool: ...


class PostgresCallsDiscoveryScheduler:
    def __init__(
        self,
        connection: AsyncConnection,
        discovery_runs: DiscoveryRunRepository,
        settings: Settings,
        runtime: RuntimeState,
        clock: Clock,
    ) -> None:
        self._connection = connection
        self._discovery_runs = discovery_runs
        self._settings = settings
        self._runtime = runtime
        self._clock = clock

    async def schedule(self) -> bool:
        initial_window_from = self._settings.discovery_start_at or self._runtime.started_at
        if initial_window_from is None:
            raise RuntimeError("Application startup time is not initialized")

        async with self._connection.begin():
            run_id = await self._discovery_runs.enqueue_next(
                initial_window_from=initial_window_from,
                window_to=self._clock.now(),
                backfill_from=self._settings.discovery_start_at,
            )
        if run_id is None:
            logger.debug("discovery scheduling skipped; no new run needed")
            return False
        logger.info("discovery run queued", extra={"run_id": str(run_id)})
        return True
