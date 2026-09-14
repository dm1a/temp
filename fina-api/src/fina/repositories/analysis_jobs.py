from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.domain.audio_contracts import CallIdentity
from fina.repositories.types import AnalysisJobClaim


class AnalysisJobRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def claim_next(self, *, worker_id: str) -> AnalysisJobClaim | None:
        """Return a processable claim; malformed identities are marked failed."""
        result = await self._connection.execute(
            text(
                """
                WITH next_job AS (
                    SELECT
                        job.call_id,
                        job.object_key,
                        call.source_call_id,
                        call.started_at,
                        call.advisor_phone,
                        call.counterparty_phone,
                        call.call_direction,
                        call.client_id
                    FROM audio_analysis_jobs AS job
                    JOIN calls AS call ON call.id = job.call_id
                    WHERE job.status = 'PENDING'
                      AND job.available_at <= now()
                    ORDER BY job.available_at, job.created_at, job.call_id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT 1
                )
                UPDATE audio_analysis_jobs AS job
                SET status = 'IN_PROGRESS',
                    claim_token = :claim_token,
                    attempts = job.attempts + 1,
                    locked_by = :worker_id,
                    locked_at = now()
                FROM next_job
                WHERE job.call_id = next_job.call_id
                RETURNING
                    job.call_id,
                    job.claim_token,
                    next_job.object_key,
                    next_job.source_call_id,
                    next_job.started_at,
                    next_job.advisor_phone,
                    next_job.counterparty_phone,
                    next_job.call_direction,
                    next_job.client_id,
                    job.attempts
                """
            ),
            {"worker_id": worker_id, "claim_token": uuid4()},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        try:
            identity = CallIdentity(
                source_call_id=row["source_call_id"],
                started_at=row["started_at"],
                advisor_phone=row["advisor_phone"],
                counterparty_phone=row["counterparty_phone"],
                call_direction=row["call_direction"],
            )
        except ValidationError:
            # Record a terminal failure so this row cannot block later claims.
            await self.mark_failed(
                call_id=row["call_id"],
                worker_id=worker_id,
                claim_token=row["claim_token"],
                error_code="INVALID_CALL_IDENTITY",
                error_message="Stored call identity is invalid",
            )
            return None

        return AnalysisJobClaim(
            call_id=row["call_id"],
            claim_token=row["claim_token"],
            identity=identity,
            manifest_object_key=row["object_key"],
            client_id=row["client_id"],
            attempts=row["attempts"],
        )

    async def complete_and_index(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        claim_token: UUID,
        analysis_result: dict[str, Any],
        analysis_schema_version: str,
        transcript_text: str,
        summary_text: str,
        client_profile_text: str,
        orders_text: str,
        search_schema_version: str,
    ) -> bool:
        statement = text(
            """
            WITH completed AS (
                UPDATE audio_analysis_jobs
                SET status = 'COMPLETED',
                    analysis_result = :analysis_result,
                    analysis_schema_version = :analysis_schema_version,
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = NULL,
                    last_error_http_status = NULL,
                    last_error_message = NULL
                WHERE call_id = :call_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                RETURNING call_id
            ),
            indexed AS (
                INSERT INTO call_search (
                    call_id,
                    transcript_text,
                    summary_text,
                    client_profile_text,
                    orders_text,
                    search_schema_version,
                    indexed_at
                )
                SELECT
                    call_id,
                    :transcript_text,
                    :summary_text,
                    :client_profile_text,
                    :orders_text,
                    :search_schema_version,
                    now()
                FROM completed
                ON CONFLICT (call_id)
                DO UPDATE SET
                    transcript_text = EXCLUDED.transcript_text,
                    summary_text = EXCLUDED.summary_text,
                    client_profile_text = EXCLUDED.client_profile_text,
                    orders_text = EXCLUDED.orders_text,
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
                "claim_token": claim_token,
                "analysis_result": analysis_result,
                "analysis_schema_version": analysis_schema_version,
                "transcript_text": transcript_text,
                "summary_text": summary_text,
                "client_profile_text": client_profile_text,
                "orders_text": orders_text,
                "search_schema_version": search_schema_version,
            },
        )
        return bool(result.scalar_one())

    async def schedule_retry(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        claim_token: UUID,
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
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
                    last_error_http_status = :error_http_status,
                    last_error_message = :error_message
                WHERE call_id = :call_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                """
            ),
            {
                "call_id": call_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
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
        claim_token: UUID,
        error_code: str,
        error_message: str,
        error_http_status: int | None = None,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE audio_analysis_jobs
                SET status = 'FAILED',
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
                    last_error_http_status = :error_http_status,
                    last_error_message = :error_message
                WHERE call_id = :call_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                """
            ),
            {
                "call_id": call_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
                "error_code": error_code,
                "error_http_status": error_http_status,
                "error_message": error_message,
            },
        )
        return result.rowcount == 1

    async def recover_stale(self, *, locked_before: datetime) -> list[UUID]:
        """Returns the call_ids of every job it reclaimed, so a caller can
        log which jobs were recovered -- not just how many."""
        result = await self._connection.execute(
            text(
                """
                UPDATE audio_analysis_jobs
                SET status = 'PENDING',
                    available_at = now(),
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status = 'IN_PROGRESS'
                  AND locked_at < :locked_before
                RETURNING call_id
                """
            ),
            {"locked_before": locked_before},
        )
        return list(result.scalars())
