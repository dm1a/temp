import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from fina.domain.audio_contracts import CallIdentityMismatchError
from fina.domain.audio_fetch import FetchCallInput, FetchCallResult
from fina.domain.clock import Clock
from fina.repositories.fetch_jobs import FetchJobRepository
from fina.repositories.types import FetchJobClaim
from fina.services.audio_completion import FetchCompletion
from fina.services.audio_fetcher import AudioFetcher
from fina.services.worker_logging import job_context, log_job_update
from fina.services.worker_loop import DEFAULT_PROCESSING_TIMEOUT, run_polling_loop

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(seconds=2)
DEFAULT_STALE_AFTER = timedelta(minutes=10)
DEFAULT_MAX_ATTEMPTS = 5


class FetchWorker:
    """Claims audio_fetch_jobs and drives them through an AudioFetcher adapter.

    Safe to run one instance per replica: claiming uses PostgreSQL's
    FOR UPDATE SKIP LOCKED (via FetchJobRepository.claim_next), so replicas
    never claim the same job, and claim tokens fence a result from a worker
    that lost its claim (e.g. reclaimed by recover_stale after a crash).
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        fetcher: AudioFetcher,
        clock: Clock,
        worker_id: str,
        poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        processing_timeout: timedelta = DEFAULT_PROCESSING_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._engine = engine
        self._fetcher = fetcher
        self._clock = clock
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._stale_after = stale_after
        self._processing_timeout = processing_timeout
        self._max_attempts = max_attempts

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await run_polling_loop(
            stop_event,
            iteration=lambda: self._iteration(stop_event),
            poll_interval=self._poll_interval,
            worker_id=self._worker_id,
            worker_kind="fetch",
        )

    async def _iteration(self, stop_event: asyncio.Event) -> bool:
        await self._recover_stale()
        if stop_event.is_set():
            return False
        return await self.run_once()

    async def _recover_stale(self) -> None:
        async with self._engine.connect() as connection, connection.begin():
            recovered = await FetchJobRepository(connection).recover_stale(
                locked_before=self._clock.now() - self._stale_after
            )
        if recovered:
            logger.warning(
                "recovered stale fetch job claims",
                extra={
                    "count": len(recovered),
                    "call_ids": [str(call_id) for call_id in recovered],
                    "worker_id": self._worker_id,
                },
            )

    async def run_once(self) -> bool:
        """Claim and process a single job. Returns False if none was available."""

        claim = await self._claim()
        if claim is None:
            return False

        logger.info("fetch job claimed", extra=job_context(self._worker_id, claim))
        try:
            async with asyncio.timeout(self._processing_timeout.total_seconds()):
                outcome = await self._fetcher.fetch_call(
                    FetchCallInput(source_call_id=claim.identity.source_call_id)
                )
                if isinstance(outcome, FetchCallResult):
                    completion = FetchCompletion(claim=claim, result=outcome)
                    await self._complete(completion)
                else:
                    await self._fail_or_retry(
                        claim,
                        error_code=outcome.error_code,
                        error_message=outcome.error_message,
                        error_http_status=outcome.http_status,
                        retryable=outcome.retryable,
                    )
        except asyncio.CancelledError:
            logger.warning(
                "fetch job cancelled; unfinished claim will be recovered",
                extra=job_context(self._worker_id, claim),
            )
            raise
        except TimeoutError:
            await self._fail_or_retry(
                claim,
                error_code="FETCH_TIMEOUT",
                error_message="fetch processing deadline exceeded",
                error_http_status=None,
                retryable=True,
            )
        except CallIdentityMismatchError as error:
            # Retrying cannot fix a fetch that returned someone else's call.
            await self._fail_or_retry(
                claim,
                error_code="IDENTITY_MISMATCH",
                error_message=str(error),
                error_http_status=None,
                retryable=False,
            )
        except Exception as error:
            logger.exception(
                "fetch job raised an unexpected error", extra=job_context(self._worker_id, claim)
            )
            await self._fail_or_retry(
                claim,
                error_code="FETCH_UNEXPECTED_ERROR",
                error_message=str(error),
                error_http_status=None,
                retryable=True,
            )
        return True

    async def _claim(self) -> FetchJobClaim | None:
        async with self._engine.connect() as connection, connection.begin():
            return await FetchJobRepository(connection).claim_next(worker_id=self._worker_id)

    async def _complete(self, completion: FetchCompletion) -> None:
        claim = completion.claim
        async with self._engine.connect() as connection, connection.begin():
            applied = await FetchJobRepository(connection).complete_and_enqueue_analysis(
                call_id=claim.call_id,
                worker_id=self._worker_id,
                claim_token=claim.claim_token,
                object_key=completion.result.manifest_object_key,
            )
        log_job_update(
            logger,
            "fetch job completed; analysis queued",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
        )

    async def _fail_or_retry(
        self,
        claim: FetchJobClaim,
        *,
        error_code: str,
        error_message: str,
        error_http_status: int | None,
        retryable: bool,
    ) -> None:
        should_retry = retryable and claim.attempts < self._max_attempts
        async with self._engine.connect() as connection, connection.begin():
            fetch_jobs = FetchJobRepository(connection)
            if should_retry:
                # Provider retries are exhausted; requeue for the next worker poll.
                applied = await fetch_jobs.schedule_retry(
                    call_id=claim.call_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    available_at=self._clock.now(),
                    error_code=error_code,
                    error_message=error_message,
                    error_http_status=error_http_status,
                )
            else:
                applied = await fetch_jobs.mark_failed(
                    call_id=claim.call_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    error_code=error_code,
                    error_message=error_message,
                    error_http_status=error_http_status,
                )

        log_job_update(
            logger,
            "fetch job retry scheduled" if should_retry else "fetch job failed",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
            level=logging.WARNING if should_retry else logging.ERROR,
            error_code=error_code,
            http_status=error_http_status,
            retryable=retryable,
            max_attempts=self._max_attempts,
        )
