from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina_api.repositories.types import DiscoveryRunClaim

CALLS_DISCOVERY_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('fina:calls-discovery', 0))"
)

ENQUEUE_NEXT_DISCOVERY_SQL = text(
    """
    WITH last_completed AS (
        SELECT MAX(window_to) AS window_from
        FROM discovery_runs
        WHERE status = 'COMPLETED'
    )
    INSERT INTO discovery_runs (id, window_from, window_to)
    SELECT
        :id,
        COALESCE(last_completed.window_from, :initial_window_from),
        :window_to
    FROM last_completed
    WHERE COALESCE(last_completed.window_from, :initial_window_from) < :window_to
      AND NOT EXISTS (
          SELECT 1
          FROM discovery_runs
          WHERE status IN ('PENDING', 'RUNNING')
      )
    ON CONFLICT (window_from, window_to) DO NOTHING
    RETURNING id
    """
)


class DiscoveryRunRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def create_or_get(self, *, window_from: datetime, window_to: datetime) -> UUID:
        proposed_id = uuid4()
        result = await self._connection.execute(
            text(
                """
                INSERT INTO discovery_runs (id, window_from, window_to)
                VALUES (:id, :window_from, :window_to)
                ON CONFLICT (window_from, window_to)
                DO UPDATE SET window_to = EXCLUDED.window_to
                RETURNING id
                """
            ),
            {
                "id": proposed_id,
                "window_from": window_from,
                "window_to": window_to,
            },
        )
        return result.scalar_one()

    async def enqueue_next(
        self,
        *,
        initial_window_from: datetime,
        window_to: datetime,
    ) -> UUID | None:
        """Create the next non-overlapping discovery run inside a caller transaction."""

        await self._connection.execute(CALLS_DISCOVERY_LOCK_SQL)
        proposed_id = uuid4()
        result = await self._connection.execute(
            ENQUEUE_NEXT_DISCOVERY_SQL,
            {
                "id": proposed_id,
                "initial_window_from": initial_window_from,
                "window_to": window_to,
            },
        )
        return result.scalar_one_or_none()

    async def claim_next(self, *, worker_id: str) -> DiscoveryRunClaim | None:
        result = await self._connection.execute(
            text(
                """
                WITH next_run AS (
                    SELECT id
                    FROM discovery_runs
                    WHERE status = 'PENDING'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE discovery_runs AS run
                SET status = 'RUNNING',
                    locked_by = :worker_id,
                    locked_at = now(),
                    error_code = NULL,
                    error_message = NULL
                FROM next_run
                WHERE run.id = next_run.id
                RETURNING run.id, run.window_from, run.window_to
                """
            ),
            {"worker_id": worker_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return DiscoveryRunClaim(
            id=row["id"],
            window_from=row["window_from"],
            window_to=row["window_to"],
        )

    async def mark_completed(self, *, run_id: UUID, worker_id: str) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'COMPLETED',
                    locked_by = NULL,
                    locked_at = NULL,
                    error_code = NULL,
                    error_message = NULL
                WHERE id = :run_id
                  AND status = 'RUNNING'
                  AND locked_by = :worker_id
                """
            ),
            {"run_id": run_id, "worker_id": worker_id},
        )
        return result.rowcount == 1

    async def mark_failed(
        self,
        *,
        run_id: UUID,
        worker_id: str,
        error_code: str,
        error_message: str,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'FAILED',
                    locked_by = NULL,
                    locked_at = NULL,
                    error_code = :error_code,
                    error_message = :error_message
                WHERE id = :run_id
                  AND status = 'RUNNING'
                  AND locked_by = :worker_id
                """
            ),
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        return result.rowcount == 1

    async def recover_stale(self, *, locked_before: datetime) -> int:
        result = await self._connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'PENDING',
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status = 'RUNNING'
                  AND locked_at < :locked_before
                """
            ),
            {"locked_before": locked_before},
        )
        return result.rowcount
