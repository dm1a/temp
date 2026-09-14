import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.audio_analysis import PreOrder, SendOrderError, SendOrderResult
from fina.domain.clock import SystemClock
from fina.domain.enums import OrderType
from fina.repositories.send_order_jobs import SendOrderJobRepository
from fina.services.send_order_worker import SendOrderWorker
from tests.integration.helpers import CLIENT, seed_call
from tests.test_calls_discovery import FixedClock

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, tzinfo=UTC)

SAMPLE_PRE_ORDER = PreOrder(
    order_type=OrderType.BUY,
    instrument_name="Сбербанк",
    volume="100 лотов",
    price="Рыночная",
    currency="RUB",
).model_dump(mode="json")


class FakeSendOrderAnalyzer:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.requests: list[tuple[UUID, str, PreOrder]] = []

    async def send_order(self, *, order_id: UUID, client_phone: str, pre_order: PreOrder):
        self.requests.append((order_id, client_phone, pre_order))
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


async def seed_send_order_job(connection, source_id: str = "call-1") -> UUID:
    call_id = await seed_call(connection, source_id)
    await SendOrderJobRepository(connection).enqueue(
        call_id=call_id, pre_order=SAMPLE_PRE_ORDER, client_phone=CLIENT
    )
    return call_id


async def send_order_job_row(connection, call_id: UUID) -> dict:
    return (
        (
            await connection.execute(
                text("SELECT * FROM send_order_jobs WHERE call_id = :call_id"),
                {"call_id": call_id},
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


def test_successful_send_order_completes_job(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)

            analyzer = FakeSendOrderAnalyzer(SendOrderResult())
            worker = SendOrderWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            assert len(analyzer.requests) == 1
            order_id, client_phone, pre_order = analyzer.requests[0]
            assert order_id == call_id
            assert client_phone == CLIENT
            assert pre_order.instrument_name == "Сбербанк"

            async with engine.connect() as connection:
                job = await send_order_job_row(connection, call_id)
                assert job["status"] == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retryable_failure_schedules_immediate_retry(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)

            analyzer = FakeSendOrderAnalyzer(
                SendOrderError(error_code="network_error", retryable=True)
            )
            worker = SendOrderWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await send_order_job_row(connection, call_id)
                assert job["status"] == "PENDING"
                assert job["attempts"] == 1
                assert job["available_at"] == NOW
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_exhausted_attempts_marks_job_failed(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)

            analyzer = FakeSendOrderAnalyzer(
                SendOrderError(error_code="network_error", retryable=True)
            )
            worker = SendOrderWorker(
                engine=engine,
                analyzer=analyzer,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                max_attempts=1,
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await send_order_job_row(connection, call_id)
                assert job["status"] == "FAILED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_non_retryable_failure_marks_job_failed_immediately(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)

            analyzer = FakeSendOrderAnalyzer(
                SendOrderError(error_code="internal_error", retryable=False)
            )
            worker = SendOrderWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await send_order_job_row(connection, call_id)
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "internal_error"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unexpected_exception_is_retried(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)

            analyzer = FakeSendOrderAnalyzer(RuntimeError("boom"))
            worker = SendOrderWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await send_order_job_row(connection, call_id)
                assert job["status"] == "PENDING"
                assert job["last_error_code"] == "SEND_ORDER_UNEXPECTED_ERROR"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_once_returns_false_when_no_jobs_available(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            worker = SendOrderWorker(
                engine=engine,
                analyzer=FakeSendOrderAnalyzer(SendOrderResult()),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )
            assert await worker.run_once() is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enqueue_is_idempotent_for_the_same_call(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)
                await SendOrderJobRepository(connection).enqueue(
                    call_id=call_id, pre_order=SAMPLE_PRE_ORDER, client_phone=CLIENT
                )

            async with engine.connect() as connection:
                count = (
                    await connection.execute(
                        text("SELECT count(*) FROM send_order_jobs WHERE call_id = :id"),
                        {"id": call_id},
                    )
                ).scalar_one()
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_forever_recovers_a_stale_claim_from_a_crashed_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_send_order_job(connection)
                crashed = await SendOrderJobRepository(connection).claim_next(worker_id="crashed")
                assert crashed is not None

            analyzer = FakeSendOrderAnalyzer(SendOrderResult())
            worker = SendOrderWorker(
                engine=engine,
                analyzer=analyzer,
                clock=SystemClock(),
                worker_id="recoverer",
                poll_interval=timedelta(milliseconds=20),
                stale_after=timedelta(seconds=0),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await send_order_job_row(connection, call_id)
                    return job["status"] == "COMPLETED"

            await wait_until(is_completed)
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
