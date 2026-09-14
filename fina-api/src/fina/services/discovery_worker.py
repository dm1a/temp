import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine

from fina.domain.audio_fetch import DiscoveredCall
from fina.domain.clock import Clock
from fina.domain.enums import ProcessingDecision
from fina.repositories.advisors import AdvisorRepository
from fina.repositories.calls import CallRepository
from fina.repositories.clients import ClientRepository
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.repositories.fetch_jobs import FetchJobRepository
from fina.repositories.types import DiscoveryRunClaim
from fina.services.audio_fetcher import AudioFetcher
from fina.services.worker_logging import job_context, log_job_update
from fina.services.worker_loop import DEFAULT_PROCESSING_TIMEOUT, run_polling_loop

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(seconds=2)
DEFAULT_STALE_AFTER = timedelta(minutes=30)

INTERNAL_CALL_SKIP_REASON = "counterparty is a registered advisor"


class DiscoveryWorker:
    """Claims discovery_runs and populates calls via an AudioFetcher adapter.

    For each active advisor, lists calls in the claimed window and stores
    each one: PROCESS with a resolved client and a queued fetch job, or SKIP
    when the counterparty phone is itself a registered advisor (an internal
    call, not a client call). add_discovered_call() and FetchJobRepository's
    create() are both idempotent upserts, so a run that fails partway through
    can be retried for the same window without reprocessing already-stored
    calls.

    Safe to run one instance per replica: claiming uses PostgreSQL's
    FOR UPDATE SKIP LOCKED (via DiscoveryRunRepository.claim_next), so
    replicas never claim the same run.
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
    ) -> None:
        self._engine = engine
        self._fetcher = fetcher
        self._clock = clock
        self._worker_id = worker_id
        self._poll_interval = poll_interval
        self._stale_after = stale_after
        self._processing_timeout = processing_timeout

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        await run_polling_loop(
            stop_event,
            iteration=lambda: self._iteration(stop_event),
            poll_interval=self._poll_interval,
            worker_id=self._worker_id,
            worker_kind="discovery",
        )

    async def _iteration(self, stop_event: asyncio.Event) -> bool:
        await self._recover_stale()
        if stop_event.is_set():
            return False
        return await self.run_once()

    async def _recover_stale(self) -> None:
        async with self._engine.connect() as connection, connection.begin():
            recovered = await DiscoveryRunRepository(connection).recover_stale(
                locked_before=self._clock.now() - self._stale_after
            )
        if recovered:
            logger.warning(
                "recovered stale discovery run claims",
                extra={
                    "count": len(recovered),
                    "run_ids": [str(run_id) for run_id in recovered],
                    "worker_id": self._worker_id,
                },
            )

    async def run_once(self) -> bool:
        """Claim and process a single discovery run. Returns False if none was available."""

        claim = await self._claim()
        if claim is None:
            return False

        logger.info("discovery job claimed", extra=job_context(self._worker_id, claim))
        try:
            async with asyncio.timeout(self._processing_timeout.total_seconds()):
                await self._process(claim)
                await self._complete(claim)
        except asyncio.CancelledError:
            logger.warning(
                "discovery job cancelled; unfinished claim will be recovered",
                extra=job_context(self._worker_id, claim),
            )
            raise
        except TimeoutError:
            await self._fail(
                claim,
                error_code="DISCOVERY_TIMEOUT",
                error_message="discovery processing deadline exceeded",
            )
        except Exception as error:
            logger.exception(
                "discovery run raised an unexpected error",
                extra=job_context(self._worker_id, claim),
            )
            await self._fail(
                claim, error_code="DISCOVERY_UNEXPECTED_ERROR", error_message=str(error)
            )
        return True

    async def _claim(self) -> DiscoveryRunClaim | None:
        async with self._engine.connect() as connection, connection.begin():
            return await DiscoveryRunRepository(connection).claim_next(worker_id=self._worker_id)

    async def _process(self, claim: DiscoveryRunClaim) -> None:
        async with self._engine.connect() as connection, connection.begin():
            active_phones = await AdvisorRepository(connection).list_active_phones()

        call_count = 0
        for advisor_phone in active_phones:
            discovered = await self._fetcher.list_available_calls(
                advisor_phone=advisor_phone,
                window_from=claim.window_from,
                window_to=claim.window_to,
            )
            call_count += len(discovered)
            for call in discovered:
                await self._store(claim, call)
        logger.info(
            "discovery calls processed",
            extra={
                **job_context(self._worker_id, claim),
                "advisor_count": len(active_phones),
                "call_count": call_count,
            },
        )

    async def _store(self, claim: DiscoveryRunClaim, call: DiscoveredCall) -> None:
        identity = call.identity
        async with self._engine.connect() as connection, connection.begin():
            advisors = AdvisorRepository(connection)
            calls = CallRepository(connection)
            is_internal = bool(await advisors.get_active_phones({identity.counterparty_phone}))
            if is_internal:
                await calls.add_discovered_call(
                    source_call_id=identity.source_call_id,
                    discovered_in_run_id=claim.id,
                    started_at=identity.started_at,
                    advisor_phone=identity.advisor_phone,
                    counterparty_phone=identity.counterparty_phone,
                    call_direction=identity.call_direction,
                    mts_filename=call.mts_filename,
                    call_duration_sec=call.call_duration_sec,
                    rec_duration_sec=call.rec_duration_sec,
                    processing_decision=ProcessingDecision.SKIP,
                    client_id=None,
                    skip_reason=INTERNAL_CALL_SKIP_REASON,
                )
                return

            clients = ClientRepository(connection)
            client_id = await clients.get_or_create(identity.counterparty_phone)
            call_id = await calls.add_discovered_call(
                source_call_id=identity.source_call_id,
                discovered_in_run_id=claim.id,
                started_at=identity.started_at,
                advisor_phone=identity.advisor_phone,
                counterparty_phone=identity.counterparty_phone,
                call_direction=identity.call_direction,
                mts_filename=call.mts_filename,
                call_duration_sec=call.call_duration_sec,
                rec_duration_sec=call.rec_duration_sec,
                processing_decision=ProcessingDecision.PROCESS,
                client_id=client_id,
                skip_reason=None,
            )
            await clients.set_origin_if_missing(client_id=client_id, call_id=call_id)
            await FetchJobRepository(connection).create(call_id=call_id)

    async def _complete(self, claim: DiscoveryRunClaim) -> None:
        async with self._engine.connect() as connection, connection.begin():
            applied = await DiscoveryRunRepository(connection).mark_completed(
                run_id=claim.id, worker_id=self._worker_id, claim_token=claim.claim_token
            )
        log_job_update(
            logger,
            "discovery run completed",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
        )

    async def _fail(self, claim: DiscoveryRunClaim, *, error_code: str, error_message: str) -> None:
        async with self._engine.connect() as connection, connection.begin():
            applied = await DiscoveryRunRepository(connection).mark_failed(
                run_id=claim.id,
                worker_id=self._worker_id,
                claim_token=claim.claim_token,
                error_code=error_code,
                error_message=error_message,
            )
        log_job_update(
            logger,
            "discovery run failed",
            applied=applied,
            worker_id=self._worker_id,
            claim=claim,
            level=logging.ERROR,
            error_code=error_code,
        )
