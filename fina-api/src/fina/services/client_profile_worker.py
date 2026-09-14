import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from fina.domain.audio_analysis import AggregateProfileResult, ClientProfile
from fina.domain.clock import Clock
from fina.repositories.client_profile_jobs import ClientProfileJobRepository
from fina.repositories.clients import ClientRepository
from fina.repositories.types import ClientProfileJobClaim
from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.worker_logging import job_context, log_job_update
from fina.services.worker_loop import DEFAULT_PROCESSING_TIMEOUT, run_polling_loop

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(seconds=2)
DEFAULT_STALE_AFTER = timedelta(minutes=10)
DEFAULT_PROFILE_HISTORY_LIMIT = 10
DEFAULT_MAX_ATTEMPTS = 5


class ClientProfileWorker:
    """Claims client_profile_jobs and drives them through an AudioAnalyzer adapter.

    Enqueued by AnalysisWorker after each successful analysis. A retryable
    failure goes straight back to PENDING (no backoff, matching
    AnalysisWorker's policy: the AudioAnalyzer is expected to have already
    exhausted its own internal retries before returning one) up to
    max_attempts, after which it's marked FAILED.

    Safe to run one instance per replica: claiming uses PostgreSQL's
    FOR UPDATE SKIP LOCKED, so replicas never claim the same job.
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        analyzer: AudioAnalyzer,
        clock: Clock,
        worker_id: str,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        processing_timeout: timedelta = DEFAULT_PROCESSING_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        profile_history_limit: int = DEFAULT_PROFILE_HISTORY_LIMIT,
    ) -> None:
        self._engine = engine
        self._analyzer = analyzer
        self._clock = clock
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._stale_after = stale_after
        self._processing_timeout = processing_timeout
        self._max_attempts = max_attempts
        self._profile_history_limit = profile_history_limit

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await run_polling_loop(
            stop_event,
            iteration=lambda: self._iteration(stop_event),
            poll_interval=self._poll_interval,
            worker_id=self._worker_id,
            worker_kind="client_profile",
        )

    async def _iteration(self, stop_event: asyncio.Event) -> bool:
        await self._recover_stale()
        if stop_event.is_set():
            return False
        return await self.run_once()

    async def _recover_stale(self) -> None:
        async with self._engine.connect() as connection, connection.begin():
            recovered = await ClientProfileJobRepository(connection).recover_stale(
                locked_before=self._clock.now() - self._stale_after
            )
        if recovered:
            logger.warning(
                "recovered stale client profile job claims",
                extra={
                    "count": len(recovered),
                    "client_ids": [str(client_id) for client_id in recovered],
                    "worker_id": self._worker_id,
                },
            )

    async def run_once(self) -> bool:
        """Claim and process a single job. Returns False if none was available."""

        claim = await self._claim()
        if claim is None:
            return False

        logger.info("client profile job claimed", extra=job_context(self._worker_id, claim))
        try:
            async with asyncio.timeout(self._processing_timeout.total_seconds()):
                profiles = await self._recent_profiles(claim)
                if not profiles:
                    await self._complete(claim)
                    return True

                outcome = await self._analyzer.aggregate_client_profile(
                    task_id=claim.client_id, profiles=profiles
                )
                if isinstance(outcome, AggregateProfileResult):
                    async with self._engine.connect() as connection, connection.begin():
                        completed = await ClientProfileJobRepository(
                            connection
                        ).complete_with_profile(
                            client_id=claim.client_id,
                            worker_id=self._worker_id,
                            claim_token=claim.claim_token,
                            claimed_request_version=claim.request_version,
                            customer_profile=outcome.profile.data,
                        )
                    log_job_update(
                        logger,
                        "client profile updated",
                        applied=completed,
                        worker_id=self._worker_id,
                        claim=claim,
                        profile_count=len(profiles),
                    )
                else:
                    await self._fail_or_retry(
                        claim,
                        error_code=outcome.error_code,
                        error_message=f"analyzer rejected client profile: {outcome.error_code}",
                        retryable=outcome.retryable,
                    )
        except asyncio.CancelledError:
            logger.warning(
                "client profile job cancelled; unfinished claim will be recovered",
                extra=job_context(self._worker_id, claim),
            )
            raise
        except TimeoutError:
            await self._fail_or_retry(
                claim,
                error_code="CLIENT_PROFILE_TIMEOUT",
                error_message="client profile processing deadline exceeded",
                retryable=True,
            )
        except Exception as error:
            logger.exception(
                "client profile job raised an unexpected error",
                extra=job_context(self._worker_id, claim),
            )
            await self._fail_or_retry(
                claim,
                error_code="CLIENT_PROFILE_UNEXPECTED_ERROR",
                error_message=str(error),
                retryable=True,
            )
        return True

    async def _recent_profiles(self, claim: ClientProfileJobClaim) -> list[ClientProfile]:
        async with self._engine.connect() as connection, connection.begin():
            profile_dicts = await ClientRepository(connection).list_recent_client_profiles(
                client_id=claim.client_id, limit=self._profile_history_limit
            )
        return [ClientProfile(data=data) for data in profile_dicts]

    async def _claim(self) -> ClientProfileJobClaim | None:
        async with self._engine.connect() as connection, connection.begin():
            return await ClientProfileJobRepository(connection).claim_next(
                worker_id=self._worker_id
            )

    async def _complete(self, claim: ClientProfileJobClaim) -> None:
        async with self._engine.connect() as connection, connection.begin():
            applied = await ClientProfileJobRepository(connection).mark_completed(
                client_id=claim.client_id,
                worker_id=self._worker_id,
                claim_token=claim.claim_token,
                claimed_request_version=claim.request_version,
            )
        log_job_update(
            logger,
            "client profile refresh finished without history",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
        )

    async def _fail_or_retry(
        self,
        claim: ClientProfileJobClaim,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        should_retry = retryable and claim.attempts < self._max_attempts
        async with self._engine.connect() as connection, connection.begin():
            jobs = ClientProfileJobRepository(connection)
            if should_retry:
                applied = await jobs.schedule_retry(
                    client_id=claim.client_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    available_at=self._clock.now(),
                    error_code=error_code,
                    error_message=error_message,
                )
            else:
                applied = await jobs.mark_failed(
                    client_id=claim.client_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                    error_code=error_code,
                    error_message=error_message,
                )

        log_job_update(
            logger,
            "client profile job retry scheduled"
            if should_retry
            else "client profile failure recorded",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
            level=logging.WARNING if should_retry else logging.ERROR,
            error_code=error_code,
            retryable=retryable,
            max_attempts=self._max_attempts,
        )
