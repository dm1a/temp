from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.repositories.types import SendOrderJobClaim


class SendOrderJobRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def enqueue(self, *, call_id: UUID, pre_order: dict[str, Any], client_phone: str) -> None:
        """Idempotent: a call has at most one pre_order, so a second enqueue is a no-op."""
        statement = text(
            """
            INSERT INTO send_order_jobs (call_id, pre_order, client_phone)
            VALUES (:call_id, :pre_order, :client_phone)
            ON CONFLICT (call_id) DO NOTHING
            """
        ).bindparams(bindparam("pre_order", type_=JSONB))
        await self._connection.execute(
            statement,
            {"call_id": call_id, "pre_order": pre_order, "client_phone": client_phone},
        )

    async def claim_next(self, *, worker_id: str) -> SendOrderJobClaim | None:
        result = await self._connection.execute(
            text(
                """
                WITH next_job AS (
                    SELECT call_id
                    FROM send_order_jobs
                    WHERE status = 'PENDING'
                      AND available_at <= now()
                    ORDER BY available_at, created_at, call_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE send_order_jobs AS job
                SET status = 'IN_PROGRESS',
                    claim_token = :claim_token,
                    attempts = job.attempts + 1,
                    locked_by = :worker_id,
                    locked_at = now()
                FROM next_job
                WHERE job.call_id = next_job.call_id
                RETURNING
                    job.call_id, job.claim_token, job.pre_order, job.client_phone, job.attempts
                """
            ),
            {"worker_id": worker_id, "claim_token": uuid4()},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return SendOrderJobClaim(
            call_id=row["call_id"],
            claim_token=row["claim_token"],
            pre_order=row["pre_order"],
            client_phone=row["client_phone"],
            attempts=row["attempts"],
        )

    async def mark_completed(self, *, call_id: UUID, worker_id: str, claim_token: UUID) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE send_order_jobs
                SET status = 'COMPLETED',
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = NULL,
                    last_error_message = NULL
                WHERE call_id = :call_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                """
            ),
            {"call_id": call_id, "worker_id": worker_id, "claim_token": claim_token},
        )
        return result.rowcount == 1

    async def schedule_retry(
        self,
        *,
        call_id: UUID,
        worker_id: str,
        claim_token: UUID,
        available_at: datetime,
        error_code: str,
        error_message: str,
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE send_order_jobs
                SET status = 'PENDING',
                    available_at = :available_at,
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
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
    ) -> bool:
        result = await self._connection.execute(
            text(
                """
                UPDATE send_order_jobs
                SET status = 'FAILED',
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
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
                UPDATE send_order_jobs
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
