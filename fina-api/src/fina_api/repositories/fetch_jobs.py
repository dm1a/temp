from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina_api.repositories.types import FetchJobClaim


class FetchJobRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def create(self, *, call_id: UUID) -> None:
        await self._connection.execute(
            text(
                """
                INSERT INTO audio_fetch_jobs (call_id)
                VALUES (:call_id)
                ON CONFLICT (call_id) DO NOTHING
                """
            ),
            {"call_id": call_id},
        )

    async def claim_next(self, *, worker_id: str) -> FetchJobClaim | None:
        result = await self._connection.execute(
            text(
                """
                WITH next_job AS (
                    SELECT job.call_id, call.source_call_id
                    FROM audio_fetch_jobs AS job
                    JOIN calls AS call ON call.id = job.call_id
                    WHERE job.status = 'PENDING'
                      AND job.available_at <= now()
                    ORDER BY job.available_at, job.created_at, job.call_id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT 1
                )
                UPDATE audio_fetch_jobs AS job
                SET status = 'IN_PROGRESS',
                    attempts = job.attempts + 1,
                    locked_by = :worker_id,
                    locked_at = now()
                FROM next_job
                WHERE job.call_id = next_job.call_id
                RETURNING job.call_id, next_job.source_call_id, job.attempts
                """
            ),
            {"worker_id": worker_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return FetchJobClaim(
            call_id=row["call_id"],
            source_call_id=row["source_call_id"],
            attempts=row["attempts"],
        )

    async def complete_and_enqueue_analysis(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        object_key: str,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                WITH completed AS (
                    UPDATE audio_fetch_jobs
                    SET status = 'COMPLETED',
                        object_key = :object_key,
                        locked_by = NULL,
                        locked_at = NULL,
                        last_error_code = NULL,
                        last_error_http_status = NULL,
                        last_error_message = NULL
                    WHERE call_id = :call_id
                      AND status = 'IN_PROGRESS'
                      AND locked_by = :worker_id
                    RETURNING call_id
                ),
                enqueued AS (
                    INSERT INTO audio_analysis_jobs (call_id, object_key)
                    SELECT call_id, :object_key
                    FROM completed
                    ON CONFLICT (call_id) DO NOTHING
                    RETURNING call_id
                )
                SELECT EXISTS (SELECT 1 FROM completed) AS completed
                """
            ),
            {
                "call_id": call_id,
                "worker_id": worker_id,
                "object_key": object_key,
            },
        )
        return bool(result.scalar_one())

    async def schedule_retry(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        available_at: datetime,
        error_code: str,
        error_message: str,
        error_http_status: int | None = None,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE audio_fetch_jobs
                SET status = 'PENDING',
                    available_at = :available_at,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
                    last_error_http_status = :error_http_status,
                    last_error_message = :error_message
                WHERE call_id = :call_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                """
            ),
            {
                "call_id": call_id,
                "worker_id": worker_id,
                "available_at": available_at,
                "error_code": error_code,
                "error_http_status": error_http_status,
                "error_message": error_message,
            },
        )
        return result.rowcount == 1

    async def mark_failed(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        error_code: str,
        error_message: str,
        error_http_status: int | None = None,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE audio_fetch_jobs
                SET status = 'FAILED',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
                    last_error_http_status = :error_http_status,
                    last_error_message = :error_message
                WHERE call_id = :call_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                """
            ),
            {
                "call_id": call_id,
                "worker_id": worker_id,
                "error_code": error_code,
                "error_http_status": error_http_status,
                "error_message": error_message,
            },
        )
        return result.rowcount == 1

    async def recover_stale(self, *, locked_before: datetime) -> int:
        result = await self._connection.execute(
            text(
                """
                UPDATE audio_fetch_jobs
                SET status = 'PENDING',
                    available_at = now(),
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status = 'IN_PROGRESS'
                  AND locked_at < :locked_before
                """
            ),
            {"locked_before": locked_before},
        )
        return result.rowcount
