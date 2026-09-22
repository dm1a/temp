import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from fina.domain.audio_analysis import AnalyzeInput, AnalyzeResult, TaskIdMismatchError
from fina.domain.clock import Clock
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.client_profile_jobs import ClientProfileJobRepository
from fina.repositories.orders import OrderRepository
from fina.repositories.send_order_jobs import SendOrderJobRepository
from fina.repositories.types import AnalysisJobClaim
from fina.services.analysis_indexing import build_search_text
from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.audio_completion import AnalysisCompletion
from fina.services.worker_logging import job_context, log_job_update
from fina.services.worker_loop import DEFAULT_PROCESSING_TIMEOUT, run_polling_loop

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(seconds=2)
DEFAULT_STALE_AFTER = timedelta(minutes=10)
DEFAULT_MAX_ATTEMPTS = 5


class AnalysisWorker:
    """Claims audio_analysis_jobs and drives them through an AudioAnalyzer adapter.

    Mirrors FetchWorker: safe to run one instance per replica, since claiming
    uses PostgreSQL's FOR UPDATE SKIP LOCKED and claim tokens fence a result
    from a worker that lost its claim (e.g. reclaimed by recover_stale).

    A successful analysis enqueues a client_profile_jobs row (always) and a
    send_order_jobs row (only when a pre_order was discovered); ClientProfileWorker
    and SendOrderWorker drive those independently, each with their own retry.
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
            worker_kind="analysis",
        )

    async def _iteration(self, stop_event: asyncio.Event) -> bool:
        await self._recover_stale()
        if stop_event.is_set():
            return False
        return await self.run_once()

    async def _recover_stale(self) -> None:
        async with self._engine.connect() as connection, connection.begin():
            recovered = await AnalysisJobRepository(connection).recover_stale(
                locked_before=self._clock.now() - self._stale_after
            )
        if recovered:
            logger.warning(
                "recovered stale analysis job claims",
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

        logger.info("analysis job claimed", extra=job_context(self._worker_id, claim))
        try:
            async with asyncio.timeout(self._processing_timeout.total_seconds()):
                outcome = await self._analyzer.analyze(
                    AnalyzeInput(
                        task_id=claim.call_id, manifest_object_key=claim.manifest_object_key
                    )
                )
                if isinstance(outcome, AnalyzeResult):
                    completion = AnalysisCompletion(claim=claim, result=outcome)
                    await self._complete(completion)
                else:
                    outcome.validate_task(claim.call_id)
                    await self._fail_or_retry(
                        claim,
                        error_code=outcome.error_code,
                        error_message=outcome.error_message,
                        error_http_status=outcome.http_status,
                        retryable=outcome.retryable,
                    )
        except asyncio.CancelledError:
            logger.warning(
                "analysis job cancelled; unfinished claim will be recovered",
                extra=job_context(self._worker_id, claim),
            )
            raise
        except TimeoutError:
            await self._fail_or_retry(
                claim,
                error_code="ANALYZE_TIMEOUT",
                error_message="analysis processing deadline exceeded",
                error_http_status=None,
                retryable=True,
            )
        except TaskIdMismatchError as error:
            # Retrying cannot fix a result echoed for the wrong task.
            await self._fail_or_retry(
                claim,
                error_code="TASK_ID_MISMATCH",
                error_message=str(error),
                error_http_status=None,
                retryable=False,
            )
        except Exception as error:
            logger.exception(
                "analysis job raised an unexpected error", extra=job_context(self._worker_id, claim)
            )
            await self._fail_or_retry(
                claim,
                error_code="ANALYZE_UNEXPECTED_ERROR",
                error_message=str(error),
                error_http_status=None,
                retryable=True,
            )
        return True

    async def _claim(self) -> AnalysisJobClaim | None:
        async with self._engine.connect() as connection, connection.begin():
            return await AnalysisJobRepository(connection).claim_next(worker_id=self._worker_id)

    async def _complete(self, completion: AnalysisCompletion) -> None:
        claim = completion.claim
        result = completion.result
        search_text = build_search_text(result.artifacts)
        async with self._engine.connect() as connection, connection.begin():
            completed = await AnalysisJobRepository(connection).complete_and_index(
                call_id=claim.call_id,
                worker_id=self._worker_id,
                claim_token=claim.claim_token,
                analysis_result=result.to_storage(),
                analysis_schema_version=result.schema_version,
                **search_text.model_dump(),
            )
            if not completed:
                # A lost claim must not trigger downstream work.
                logger.warning(
                    "analysis claim was lost before completion could be applied",
                    extra=job_context(self._worker_id, claim),
                )
                return
            await ClientProfileJobRepository(connection).enqueue(client_id=claim.client_id)
            if result.artifacts.pre_order is not None:
                await OrderRepository(connection).upsert(
                    call_id=claim.call_id, pre_order=result.artifacts.pre_order
                )
                await SendOrderJobRepository(connection).enqueue(
                    call_id=claim.call_id,
                    pre_order=result.artifacts.pre_order.model_dump(mode="json"),
                    client_phone=claim.identity.counterparty_phone,
                )
        log_job_update(
            logger,
            "analysis job completed; downstream jobs queued",
            applied=completed,
            worker_id=self._worker_id,
            claim=claim,
            order_enqueued=result.artifacts.pre_order is not None,
        )

    async def _fail_or_retry(
        self,
        claim: AnalysisJobClaim,
        *,
        error_code: str,
        error_message: str,
        error_http_status: int | None,
        retryable: bool,
    ) -> None:
        should_retry = retryable and claim.attempts < self._max_attempts
        async with self._engine.connect() as connection, connection.begin():
            analysis_jobs = AnalysisJobRepository(connection)
            if should_retry:
                # Provider retries are exhausted; requeue for the next worker poll.
                applied = await analysis_jobs.schedule_retry(
                    call_id=claim.call_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    available_at=self._clock.now(),
                    error_code=error_code,
                    error_message=error_message,
                    error_http_status=error_http_status,
                )
            else:
                applied = await analysis_jobs.mark_failed(
                    call_id=claim.call_id,
                    worker_id=self._worker_id,
                    claim_token=claim.claim_token,
                    error_code=error_code,
                    error_message=error_message,
                    error_http_status=error_http_status,
                )

        log_job_update(
            logger,
            "analysis job retry scheduled" if should_retry else "analysis job failed",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
            level=logging.WARNING if should_retry else logging.ERROR,
            error_code=error_code,
            http_status=error_http_status,
            retryable=retryable,
            max_attempts=self._max_attempts,
        )
