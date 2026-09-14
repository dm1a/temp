import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from fina.domain.audio_analysis import PreOrder, SendOrderResult
from fina.domain.clock import Clock
from fina.repositories.send_order_jobs import SendOrderJobRepository
from fina.repositories.types import SendOrderJobClaim
from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.worker_logging import job_context, log_job_update
from fina.services.worker_loop import DEFAULT_PROCESSING_TIMEOUT, run_polling_loop

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(seconds=2)
DEFAULT_STALE_AFTER = timedelta(minutes=10)
DEFAULT_MAX_ATTEMPTS = 5


class SendOrderWorker:
    """Claims send_order_jobs and drives them through an AudioAnalyzer adapter.

    Enqueued by AnalysisWorker whenever an analysis discovers a pre_order. A
    retryable failure goes straight back to PENDING (no backoff, same policy
    as AnalysisWorker and ClientProfileWorker) up to max_attempts, after
    which it's marked FAILED.

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
    ) -> None:
        self._engine = engine
        self._analyzer = analyzer
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
            worker_kind="send_order",
        )

    async def _iteration(self, stop_event: asyncio.Event) -> bool:
        await self._recover_stale()
        if stop_event.is_set():
            return False
        return await self.run_once()

    async def _recover_stale(self) -> None:
        async with self._engine.connect() as connection, connection.begin():
            recovered = await SendOrderJobRepository(connection).recover_stale(
                locked_before=self._clock.now() - self._stale_after
            )
        if recovered:
            logger.warning(
                "recovered stale send order job claims",
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

        logger.info("send order job claimed", extra=job_context(self._worker_id, claim))
        try:
            async with asyncio.timeout(self._processing_timeout.total_seconds()):
                outcome = await self._analyzer.send_order(
                    order_id=claim.call_id,
                    client_phone=claim.client_phone,
                    pre_order=PreOrder.model_validate(claim.pre_order),
                )
                if isinstance(outcome, SendOrderResult):
                    await self._complete(claim)
                else:
                    await self._fail_or_retry(
                        claim,
                        error_code=outcome.error_code,
                        error_message=f"analyzer rejected send_order: {outcome.error_code}",
                        retryable=outcome.retryable,
                    )
        except asyncio.CancelledError:
            logger.warning(
                "send order job cancelled; unfinished claim will be recovered",
                extra=job_context(self._worker_id, claim),
            )
            raise
        except TimeoutError:
            await self._fail_or_retry(
                claim,
                error_code="SEND_ORDER_TIMEOUT",
                error_message="send order processing deadline exceeded",
                retryable=True,
            )
        except Exception as error:
            logger.exception(
                "send order job raised an unexpected error",
                extra=job_context(self._worker_id, claim),
            )
            await self._fail_or_retry(
                claim,
                error_code="SEND_ORDER_UNEXPECTED_ERROR",
                error_message=str(error),
                retryable=True,
            )
        return True

    async def _claim(self) -> SendOrderJobClaim | None:
        async with self._engine.connect() as connection, connection.begin():
            return await SendOrderJobRepository(connection).claim_next(worker_id=self._worker_id)

    async def _complete(self, claim: SendOrderJobClaim) -> None:
        async with self._engine.connect() as connection, connection.begin():
            applied = await SendOrderJobRepository(connection).mark_completed(
                call_id=claim.call_id, worker_id=self._worker_id, claim_token=claim.claim_token
            )
        log_job_update(
            logger,
            "send order job completed",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
        )

    async def _fail_or_retry(
        self,
        claim: SendOrderJobClaim,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> None:
        should_retry = retryable and claim.attempts < self._max_attempts
        async with self._engine.connect() as connection, connection.begin():
            jobs = SendOrderJobRepository(connection)
            if should_retry:
                applied = await jobs.schedule_retry(
                    call_id=claim.call_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    available_at=self._clock.now(),
                    error_code=error_code,
                    error_message=error_message,
                )
            else:
                applied = await jobs.mark_failed(
                    call_id=claim.call_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    error_code=error_code,
                    error_message=error_message,
                )

        log_job_update(
            logger,
            "send order job retry scheduled" if should_retry else "send order job failed",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
            level=logging.WARNING if should_retry else logging.ERROR,
            error_code=error_code,
            retryable=retryable,
            max_attempts=self._max_attempts,
        )
