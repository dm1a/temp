from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.repositories.types import DiscoveryRunClaim

CALLS_DISCOVERY_LOCK_SQL = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('fina:calls-discovery', 0))"
)

ENQUEUE_NEXT_DISCOVERY_SQL = text(
    """
    WITH bounds AS (
        SELECT
            MIN(window_from) AS earliest_from,
            MAX(window_to) FILTER (WHERE status = 'COMPLETED') AS latest_completed_to
        FROM discovery_runs
    ),
    -- Only an explicitly configured start requests backfill. A replica's startup
    -- time is a fallback for empty history, not a reason to rewind stored progress.
    boundary AS (
        SELECT
            CASE
                WHEN :backfill_from < bounds.earliest_from
                THEN :backfill_from
                ELSE COALESCE(
                    bounds.latest_completed_to, bounds.earliest_from, :initial_window_from
                )
            END AS window_from,
            CASE
                WHEN :backfill_from < bounds.earliest_from
                THEN bounds.earliest_from
                ELSE :window_to
            END AS window_to
        FROM bounds
    )
    INSERT INTO discovery_runs (id, window_from, window_to)
    SELECT
        :id,
        boundary.window_from,
        boundary.window_to
    FROM boundary
    WHERE boundary.window_from < boundary.window_to
      AND NOT EXISTS (
          SELECT 1
          FROM discovery_runs
          WHERE status IN ('PENDING', 'RUNNING')
      )
    ON CONFLICT (window_from, window_to) DO UPDATE
    SET status = 'PENDING', error_code = NULL, error_message = NULL
    WHERE discovery_runs.status = 'FAILED'
    RETURNING id
    """
)


class DiscoveryRunRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def create_or_get(self, *, window_from: datetime, window_to: datetime) -> UUID:
        # All creation paths share the scheduler's transaction-scoped lock.
        await self._connection.execute(CALLS_DISCOVERY_LOCK_SQL)
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
        backfill_from: datetime | None = None,
    ) -> UUID | None:
        """Create the next non-overlapping discovery run inside a caller transaction.

        `initial_window_from` seeds empty history. An explicit `backfill_from`
        earlier than the earliest stored window requests a run closing that gap.
        """

        await self._connection.execute(CALLS_DISCOVERY_LOCK_SQL)
        proposed_id = uuid4()
        result = await self._connection.execute(
            ENQUEUE_NEXT_DISCOVERY_SQL,
            {
                "id": proposed_id,
                "initial_window_from": initial_window_from,
                "window_to": window_to,
                "backfill_from": backfill_from,
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
                    claim_token = :claim_token,
                    locked_by = :worker_id,
                    locked_at = now(),
                    error_code = NULL,
                    error_message = NULL
                FROM next_run
                WHERE run.id = next_run.id
                RETURNING run.id, run.claim_token, run.window_from, run.window_to
                """
            ),
            {"worker_id": worker_id, "claim_token": uuid4()},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return DiscoveryRunClaim(
            id=row["id"],
            claim_token=row["claim_token"],
            window_from=row["window_from"],
            window_to=row["window_to"],
        )

    async def mark_completed(self, *, run_id: UUID, worker_id: str, claim_token: UUID) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'COMPLETED',
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    error_code = NULL,
                    error_message = NULL
                WHERE id = :run_id
                  AND status = 'RUNNING'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                """
            ),
            {"run_id": run_id, "worker_id": worker_id, "claim_token": claim_token},
        )
        return result.rowcount == 1

    async def mark_failed(
        self,
        *,
        run_id: UUID,
        worker_id: str,
        claim_token: UUID,
        error_code: str,
        error_message: str,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'FAILED',
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    error_code = :error_code,
                    error_message = :error_message
                WHERE id = :run_id
                  AND status = 'RUNNING'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                """
            ),
            {
                "run_id": run_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        return result.rowcount == 1

    async def recover_stale(self, *, locked_before: datetime) -> list[UUID]:
        """Returns the ids of every run it reclaimed, so a caller can log
        which runs were recovered -- not just how many."""
        result = await self._connection.execute(
            text(
                """
                UPDATE discovery_runs
                SET status = 'PENDING',
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status = 'RUNNING'
                  AND locked_at < :locked_before
                RETURNING id
                """
            ),
            {"locked_before": locked_before},
        )
        return list(result.scalars())
