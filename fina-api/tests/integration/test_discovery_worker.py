import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fina.domain.audio_contracts import CallIdentity
from fina.domain.audio_fetch import DiscoveredCall, FetchCallInput, FetchCallOutcome
from fina.domain.clock import SystemClock
from fina.domain.enums import CallDirection
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.services.discovery_worker import DiscoveryWorker

pytestmark = pytest.mark.integration

START = datetime(2026, 9, 1, tzinfo=UTC)
ADVISOR = "79990000001"


def discovered(
    source_call_id: str,
    *,
    counterparty_phone: str,
    advisor_phone: str = ADVISOR,
    call_direction: CallDirection = CallDirection.INBOUND,
) -> DiscoveredCall:
    return DiscoveredCall(
        identity=CallIdentity(
            source_call_id=source_call_id,
            started_at=START,
            advisor_phone=advisor_phone,
            counterparty_phone=counterparty_phone,
            call_direction=call_direction,
        ),
        mts_filename="rec.mp3",
        call_duration_sec=90,
        rec_duration_sec=88,
    )


class FakeAudioFetcher:
    def __init__(self, results: dict[str, list[DiscoveredCall] | BaseException]) -> None:
        self._results = results
        self.requested: list[str] = []

    async def fetch_call(self, input_data: FetchCallInput) -> FetchCallOutcome:
        raise AssertionError("fetch_call is not exercised by discovery worker tests")

    async def list_available_calls(
        self, *, advisor_phone: str, window_from: datetime, window_to: datetime
    ) -> list[DiscoveredCall]:
        self.requested.append(advisor_phone)
        result = self._results.get(advisor_phone, [])
        if isinstance(result, BaseException):
            raise result
        return result


async def insert_advisor(connection: AsyncConnection, phone: str) -> None:
    await connection.execute(
        text("INSERT INTO advisors (phone_normalized, is_active) VALUES (:phone, true)"),
        {"phone": phone},
    )


async def seed_pending_run(connection: AsyncConnection) -> None:
    await DiscoveryRunRepository(connection).create_or_get(
        window_from=START, window_to=START + timedelta(days=1)
    )


async def call_row(connection: AsyncConnection, source_call_id: str) -> dict:
    return (
        (
            await connection.execute(
                text(
                    "SELECT processing_decision, client_id, skip_reason "
                    "FROM calls WHERE source_call_id = :id"
                ),
                {"id": source_call_id},
            )
        )
        .mappings()
        .one()
    )


async def wait_until(condition, *, timeout: float = 5) -> None:
    async def poll() -> None:
        while not await condition():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


def test_process_creates_client_and_fetch_job_for_a_real_client_call(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await insert_advisor(connection, ADVISOR)
                await seed_pending_run(connection)

            fetcher = FakeAudioFetcher(
                {ADVISOR: [discovered("111", counterparty_phone="79990000002")]}
            )
            worker = DiscoveryWorker(
                engine=engine, fetcher=fetcher, clock=SystemClock(), worker_id="worker"
            )

            assert await worker.run_once() is True

            assert fetcher.requested == [ADVISOR]
            async with engine.connect() as connection:
                row = await call_row(connection, "111")
                assert row["processing_decision"] == "PROCESS"
                assert row["client_id"] is not None
                assert row["skip_reason"] is None

                fetch_job_status = (
                    await connection.execute(
                        text(
                            "SELECT job.status FROM audio_fetch_jobs AS job "
                            "JOIN calls ON calls.id = job.call_id "
                            "WHERE calls.source_call_id = '111'"
                        )
                    )
                ).scalar_one()
                assert fetch_job_status == "PENDING"

                client_phone = (
                    await connection.execute(text("SELECT phone_normalized FROM clients"))
                ).scalar_one()
                assert client_phone == "79990000002"

                run_status = (
                    await connection.execute(text("SELECT status FROM discovery_runs"))
                ).scalar_one()
                assert run_status == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_process_skips_internal_advisor_to_advisor_calls(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await insert_advisor(connection, ADVISOR)
                await insert_advisor(connection, "79990000003")
                await seed_pending_run(connection)

            fetcher = FakeAudioFetcher(
                {ADVISOR: [discovered("222", counterparty_phone="79990000003")]}
            )
            worker = DiscoveryWorker(
                engine=engine, fetcher=fetcher, clock=SystemClock(), worker_id="worker"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                row = await call_row(connection, "222")
                assert row["processing_decision"] == "SKIP"
                assert row["client_id"] is None
                assert row["skip_reason"] == "counterparty is a registered advisor"

                assert (
                    await connection.execute(text("SELECT count(*) FROM clients"))
                ).scalar_one() == 0
                assert (
                    await connection.execute(text("SELECT count(*) FROM audio_fetch_jobs"))
                ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_once_marks_run_failed_on_unexpected_error(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await insert_advisor(connection, ADVISOR)
                await seed_pending_run(connection)

            fetcher = FakeAudioFetcher({ADVISOR: RuntimeError("provider unavailable")})
            worker = DiscoveryWorker(
                engine=engine, fetcher=fetcher, clock=SystemClock(), worker_id="worker"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text("SELECT status, error_code FROM discovery_runs")
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "FAILED"
                assert row["error_code"] == "DISCOVERY_UNEXPECTED_ERROR"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_once_returns_false_when_no_pending_run(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            worker = DiscoveryWorker(
                engine=engine, fetcher=FakeAudioFetcher({}), clock=SystemClock(), worker_id="worker"
            )
            assert await worker.run_once() is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_forever_recovers_a_stale_claim_from_a_crashed_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await insert_advisor(connection, ADVISOR)
                await seed_pending_run(connection)
            async with engine.begin() as connection:
                crashed = await DiscoveryRunRepository(connection).claim_next(worker_id="crashed")
                assert crashed is not None

            fetcher = FakeAudioFetcher(
                {ADVISOR: [discovered("333", counterparty_phone="79990000002")]}
            )
            # locked_at is set by the database's own now(), so recovery must be
            # judged against real wall-clock time, not a fictional test clock.
            worker = DiscoveryWorker(
                engine=engine,
                fetcher=fetcher,
                clock=SystemClock(),
                worker_id="recoverer",
                poll_interval=timedelta(milliseconds=20),
                stale_after=timedelta(seconds=0),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    status = (
                        await connection.execute(text("SELECT status FROM discovery_runs"))
                    ).scalar_one()
                    return status == "COMPLETED"

            await wait_until(is_completed)
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
