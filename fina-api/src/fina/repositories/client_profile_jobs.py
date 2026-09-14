from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.repositories.types import ClientProfileJobClaim


class ClientProfileJobRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def enqueue(self, *, client_id: UUID) -> None:
        """Idempotent, and never loses a request: request_version is
        incremented on every call, even while a job is already IN_PROGRESS
        (status/available_at are only reset when the job is currently
        terminal, so an active claim is left undisturbed).

        Does not touch attempts at all -- claim_next() is the sole place
        that decides whether attempts continues a retry streak or restarts
        at 1, based on whether request_version has moved since attempts was
        last counted. That keeps the decision correct no matter how the job
        got back to PENDING (a normal retry, a redirect after a stale
        completion/failure, or recover_stale() cleaning up after a crashed
        worker)."""
        await self._connection.execute(
            text(
                """
                INSERT INTO client_profile_jobs (client_id, request_version)
                VALUES (:client_id, 1)
                ON CONFLICT (client_id) DO UPDATE
                SET request_version = client_profile_jobs.request_version + 1,
                    status = CASE
                        WHEN client_profile_jobs.status IN ('COMPLETED', 'FAILED') THEN 'PENDING'
                        ELSE client_profile_jobs.status
                    END,
                    available_at = CASE
                        WHEN client_profile_jobs.status IN ('COMPLETED', 'FAILED') THEN now()
                        ELSE client_profile_jobs.available_at
                    END
                """
            ),
            {"client_id": client_id},
        )

    async def claim_next(self, *, worker_id: str) -> ClientProfileJobClaim | None:
        """Claims the next due job. attempts continues its streak only if
        this row's request_version hasn't moved since attempts was last
        counted (attempts_request_version); otherwise -- a newer enqueue()
        arrived since the last attempt, whether or not that was noticed by
        the code that last released this claim -- it's the first attempt of
        a new generation, so attempts restarts at 1. This is what makes
        recover_stale() safe to leave attempts/request_version untouched: a
        crashed worker's stale attempt count is reinterpreted correctly the
        moment the job is reclaimed, without recover_stale() itself needing
        to know anything about versions."""
        result = await self._connection.execute(
            text(
                """
                WITH next_job AS (
                    SELECT client_id
                    FROM client_profile_jobs
                    WHERE status = 'PENDING'
                      AND available_at <= now()
                    ORDER BY available_at, created_at, client_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE client_profile_jobs AS job
                SET status = 'IN_PROGRESS',
                    claim_token = :claim_token,
                    attempts = CASE
                        WHEN job.attempts_request_version = job.request_version
                        THEN job.attempts + 1
                        ELSE 1
                    END,
                    attempts_request_version = job.request_version,
                    locked_by = :worker_id,
                    locked_at = now()
                FROM next_job
                WHERE job.client_id = next_job.client_id
                RETURNING job.client_id, job.claim_token, job.attempts, job.request_version
                """
            ),
            {"worker_id": worker_id, "claim_token": uuid4()},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return ClientProfileJobClaim(
            client_id=row["client_id"],
            claim_token=row["claim_token"],
            attempts=row["attempts"],
            request_version=row["request_version"],
        )

    async def mark_completed(
        self,
        *,
        client_id: UUID,
        worker_id: str,
        claim_token: UUID,
        claimed_request_version: int,
    ) -> bool:
        """Completes the job only if no newer enqueue() arrived since it was
        claimed (request_version unchanged); otherwise releases the claim
        back to PENDING immediately, so the newer request gets reprocessed
        rather than silently completing without it. For jobs with nothing to
        write elsewhere (no recent profiles to aggregate) -- see
        complete_with_profile() for the atomic version that also writes
        clients.customer_profile."""
        result = await self._connection.execute(
            text(
                """
                UPDATE client_profile_jobs
                SET status = CASE
                        WHEN request_version <= :claimed_request_version THEN 'COMPLETED'
                        ELSE 'PENDING'
                    END,
                    available_at = CASE
                        WHEN request_version <= :claimed_request_version THEN available_at
                        ELSE now()
                    END,
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = NULL,
                    last_error_message = NULL
                WHERE client_id = :client_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                RETURNING (request_version <= :claimed_request_version) AS newly_completed
                """
            ),
            {
                "client_id": client_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
                "claimed_request_version": claimed_request_version,
            },
        )
        row = result.mappings().one_or_none()
        return bool(row["newly_completed"]) if row is not None else False

    async def complete_with_profile(
        self,
        *,
        client_id: UUID,
        worker_id: str,
        claim_token: UUID,
        claimed_request_version: int,
        customer_profile: dict[str, Any],
    ) -> bool:
        """Atomically locks the claim, writes clients.customer_profile, and
        completes (or re-queues, per the same request_version check as
        mark_completed()) the job -- all in one statement, so the claim
        can never be lost to a concurrent recover_stale()+reclaim between
        checking it and writing the profile. A plain check-then-write
        (e.g. an unlocked EXISTS) would leave exactly that gap: another
        transaction could reassign the job and write a newer profile in
        between, which this transaction's write would then clobber on
        commit. FOR UPDATE closes it by holding the row lock across both
        the check and the write. Returns False if the claim was already
        lost or a newer request superseded it (profile is left untouched
        in either case)."""
        statement = text(
            """
            WITH locked_job AS (
                SELECT client_id, request_version
                FROM client_profile_jobs
                WHERE client_id = :client_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                FOR UPDATE
            ),
            profile_updated AS (
                UPDATE clients
                SET customer_profile = :customer_profile,
                    customer_profile_updated_at = now()
                FROM locked_job
                WHERE clients.id = locked_job.client_id
                  AND locked_job.request_version <= :claimed_request_version
            ),
            job_completed AS (
                UPDATE client_profile_jobs
                SET status = CASE
                        WHEN locked_job.request_version <= :claimed_request_version
                        THEN 'COMPLETED' ELSE 'PENDING'
                    END,
                    available_at = CASE
                        WHEN locked_job.request_version <= :claimed_request_version
                        THEN client_profile_jobs.available_at ELSE now()
                    END,
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = NULL,
                    last_error_message = NULL
                FROM locked_job
                WHERE client_profile_jobs.client_id = locked_job.client_id
                RETURNING
                    (locked_job.request_version <= :claimed_request_version) AS newly_completed
            )
            SELECT newly_completed FROM job_completed
            """
        ).bindparams(bindparam("customer_profile", type_=JSONB))
        result = await self._connection.execute(
            statement,
            {
                "client_id": client_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
                "claimed_request_version": claimed_request_version,
                "customer_profile": customer_profile,
            },
        )
        row = result.mappings().one_or_none()
        return bool(row["newly_completed"]) if row is not None else False

    async def schedule_retry(
        self,
        *,
        client_id: UUID,
        worker_id: str,
        claim_token: UUID,
        available_at: datetime,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Requeues for another attempt, regardless of whether a newer
        request has arrived since this claim was taken -- either way the
        job needs reprocessing, and claim_next() is what correctly resets
        attempts to 1 rather than continuing the streak if a newer
        request_version is picked up on the next claim."""
        result = await self._connection.execute(
            text(
                """
                UPDATE client_profile_jobs
                SET status = 'PENDING',
                    available_at = :available_at,
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = :error_code,
                    last_error_message = :error_message
                WHERE client_id = :client_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                """
            ),
            {
                "client_id": client_id,
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
        client_id: UUID,
        worker_id: str,
        claim_token: UUID,
        claimed_request_version: int,
        error_code: str,
        error_message: str,
    ) -> bool:
        """Fails the job only if no newer enqueue() arrived since it was
        claimed; otherwise re-queues instead, so a permanent failure on
        stale data doesn't discard a call that finished (and needs its own
        attempt) during processing. claim_next() gives that re-queued
        generation a fresh attempts budget when it's next claimed."""
        result = await self._connection.execute(
            text(
                """
                UPDATE client_profile_jobs
                SET status = CASE
                        WHEN request_version <= :claimed_request_version THEN 'FAILED'
                        ELSE 'PENDING'
                    END,
                    available_at = CASE
                        WHEN request_version <= :claimed_request_version THEN available_at
                        ELSE now()
                    END,
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error_code = CASE
                        WHEN request_version <= :claimed_request_version THEN :error_code
                        ELSE NULL
                    END,
                    last_error_message = CASE
                        WHEN request_version <= :claimed_request_version THEN :error_message
                        ELSE NULL
                    END
                WHERE client_id = :client_id
                  AND status = 'IN_PROGRESS'
                  AND locked_by = :worker_id
                  AND claim_token = :claim_token
                RETURNING (request_version <= :claimed_request_version) AS newly_failed
                """
            ),
            {
                "client_id": client_id,
                "worker_id": worker_id,
                "claim_token": claim_token,
                "claimed_request_version": claimed_request_version,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        row = result.mappings().one_or_none()
        return bool(row["newly_failed"]) if row is not None else False

    async def recover_stale(self, *, locked_before: datetime) -> list[UUID]:
        """Returns the client_ids of every job it reclaimed, so a caller
        can log which jobs were recovered -- not just how many."""
        result = await self._connection.execute(
            text(
                """
                UPDATE client_profile_jobs
                SET status = 'PENDING',
                    available_at = now(),
                    claim_token = NULL,
                    locked_by = NULL,
                    locked_at = NULL
                WHERE status = 'IN_PROGRESS'
                  AND locked_at < :locked_before
                RETURNING client_id
                """
            ),
            {"locked_before": locked_before},
        )
        return list(result.scalars())
