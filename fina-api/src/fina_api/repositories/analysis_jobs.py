from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from fina_api.repositories.types import AnalysisJobClaim


class AnalysisJobRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def claim_next(self, *, worker_id: str) -> AnalysisJobClaim | None:
        result = await self._connection.execute(
            text(
                """
                WITH next_job AS (
                    SELECT call_id
                    FROM audio_analysis_jobs
                    WHERE status = 'PENDING'
                      AND available_at <= now()
                    ORDER BY available_at, created_at, call_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE audio_analysis_jobs AS job
                SET status = 'IN_PROGRESS',
                    attempts = job.attempts + 1,
                    locked_by = :worker_id,
                    locked_at = now()
                FROM next_job
                WHERE job.call_id = next_job.call_id
                RETURNING job.call_id, job.object_key, job.attempts
                """
            ),
            {"worker_id": worker_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return AnalysisJobClaim(
            call_id=row["call_id"],
            object_key=row["object_key"],
            attempts=row["attempts"],
        )

    async def complete_and_index(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        analysis_result: dict[str, Any],
        analysis_schema_version: str,
        transcript_text: str,
        summary_text: str,
        client_profile_text: str,
        deals_text: str,
        search_schema_version: str,
    ) -> bool:
        statement = text(
            """
            WITH completed AS (
                UPDATE audio_analysis_jobs
                SET status = 'COMPLETED',
                    analysis_result = :analysis_result,
                    analysis_schema_version = :analysis_schema_version,
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
            indexed AS (
                INSERT INTO call_search (
                    call_id,
                    transcript_text,
                    summary_text,
                    client_profile_text,
                    deals_text,
                    search_schema_version,
                    indexed_at
                )
                SELECT
                    call_id,
                    :transcript_text,
                    :summary_text,
                    :client_profile_text,
                    :deals_text,
                    :search_schema_version,
                    now()
                FROM completed
                ON CONFLICT (call_id)
                DO UPDATE SET
                    transcript_text = EXCLUDED.transcript_text,
                    summary_text = EXCLUDED.summary_text,
                    client_profile_text = EXCLUDED.client_profile_text,
                    deals_text = EXCLUDED.deals_text,
                    search_schema_version = EXCLUDED.search_schema_version,
                    indexed_at = now()
                RETURNING call_id
            )
            SELECT EXISTS (SELECT 1 FROM completed) AS completed
            """
        ).bindparams(bindparam("analysis_result", type_=JSONB))
        result = await self._connection.execute(
            statement,
            {
                "call_id": call_id,
                "worker_id": worker_id,
                "analysis_result": analysis_result,
                "analysis_schema_version": analysis_schema_version,
                "transcript_text": transcript_text,
                "summary_text": summary_text,
                "client_profile_text": client_profile_text,
                "deals_text": deals_text,
                "search_schema_version": search_schema_version,
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
                UPDATE audio_analysis_jobs
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
                UPDATE audio_analysis_jobs
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
                UPDATE audio_analysis_jobs
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
