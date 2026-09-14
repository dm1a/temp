import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.audio_analysis import (
    AggregateProfileError,
    AggregateProfileResult,
    AnalyzeResult,
    ClientProfile,
    CustomerProfile,
)
from fina.domain.clock import SystemClock
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.client_profile_jobs import ClientProfileJobRepository
from fina.services.analysis_indexing import build_search_text
from fina.services.client_profile_worker import ClientProfileWorker
from tests.analysis_examples import analysis_data
from tests.integration.helpers import START, enqueue_analysis, seed_call
from tests.test_calls_discovery import FixedClock

pytestmark = pytest.mark.integration

NOW = START + timedelta(days=1)


class FakeProfileAnalyzer:
    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.profile_requests: list[list[ClientProfile]] = []

    async def aggregate_client_profile(self, *, task_id: UUID, profiles: list[ClientProfile]):
        self.profile_requests.append(profiles)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


async def seed_completed_analysis(connection, source_id: str) -> tuple[UUID, UUID]:
    """Seed a call through a real, fully-shaped analysis completion (as
    AnalysisWorker._complete() would produce), including the
    client_profile_jobs row it enqueues. Returns (call_id, client_id)."""
    call_id = await seed_call(connection, source_id)
    await enqueue_analysis(connection, call_id)
    data = analysis_data(call_id)
    data["identity"]["source_call_id"] = source_id
    result = AnalyzeResult.model_validate(data)

    analysis_jobs = AnalysisJobRepository(connection)
    claim = await analysis_jobs.claim_next(worker_id="seed")
    assert claim is not None and claim.call_id == call_id
    search_text = build_search_text(result.artifacts)
    await analysis_jobs.complete_and_index(
        call_id=call_id,
        worker_id="seed",
        claim_token=claim.claim_token,
        analysis_result=result.to_storage(),
        analysis_schema_version=result.schema_version,
        **search_text.model_dump(),
    )
    client_id = claim.client_id
    await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
    return call_id, client_id


async def client_profile_job_row(connection, client_id: UUID) -> dict:
    return (
        (
            await connection.execute(
                text("SELECT * FROM client_profile_jobs WHERE client_id = :client_id"),
                {"client_id": client_id},
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


async def customer_profile_row(connection, client_id: UUID) -> dict:
    return (
        (
            await connection.execute(
                text(
                    "SELECT customer_profile, customer_profile_updated_at "
                    "FROM clients WHERE id = :client_id"
                ),
                {"client_id": client_id},
            )
        )
        .mappings()
        .one()
    )


def test_successful_aggregation_updates_customer_profile_and_completes_job(
    database_url: str,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            analyzer = FakeProfileAnalyzer(
                AggregateProfileResult(profile=CustomerProfile(data={"theme_summary": "Облигации"}))
            )
            worker = ClientProfileWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            assert len(analyzer.profile_requests) == 1
            assert len(analyzer.profile_requests[0]) == 1

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "COMPLETED"
                row = await customer_profile_row(connection, client_id)
                assert row["customer_profile"] == {"theme_summary": "Облигации"}
                assert row["customer_profile_updated_at"] is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_no_history_completes_job_without_calling_analyzer(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = (
                    await connection.execute(
                        text("SELECT client_id FROM calls WHERE id = :id"), {"id": call_id}
                    )
                ).scalar_one()
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            analyzer = FakeProfileAnalyzer(AggregateProfileResult(profile=CustomerProfile(data={})))
            worker = ClientProfileWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            assert analyzer.profile_requests == []
            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "COMPLETED"
                row = await customer_profile_row(connection, client_id)
                assert row["customer_profile"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retryable_failure_schedules_immediate_retry(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            analyzer = FakeProfileAnalyzer(
                AggregateProfileError(error_code="network_error", retryable=True)
            )
            worker = ClientProfileWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
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
                _, client_id = await seed_completed_analysis(connection, "call-1")

            analyzer = FakeProfileAnalyzer(
                AggregateProfileError(error_code="network_error", retryable=True)
            )
            worker = ClientProfileWorker(
                engine=engine,
                analyzer=analyzer,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                max_attempts=1,
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "FAILED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_non_retryable_failure_marks_job_failed_immediately(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            analyzer = FakeProfileAnalyzer(
                AggregateProfileError(error_code="all_none", retryable=False)
            )
            worker = ClientProfileWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "all_none"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unexpected_exception_is_retried(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            analyzer = FakeProfileAnalyzer(RuntimeError("boom"))
            worker = ClientProfileWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "PENDING"
                assert job["last_error_code"] == "CLIENT_PROFILE_UNEXPECTED_ERROR"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_once_returns_false_when_no_jobs_available(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            worker = ClientProfileWorker(
                engine=engine,
                analyzer=FakeProfileAnalyzer(
                    AggregateProfileResult(profile=CustomerProfile(data={}))
                ),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )
            assert await worker.run_once() is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_a_newer_request_gets_its_own_full_retry_budget(database_url: str) -> None:
    """Reproduces the reported bug: a call that arrives while an earlier
    generation is being processed must not inherit that generation's
    already-spent attempts. With max_attempts=2, the new generation must
    get its own 2 attempts before being marked FAILED -- not be permanently
    failed after just one."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            analyzer = FakeProfileAnalyzer(
                AggregateProfileError(error_code="network_error", retryable=True)
            )
            worker = ClientProfileWorker(
                engine=engine,
                analyzer=analyzer,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                max_attempts=2,
            )

            async with engine.begin() as connection:
                claim_v1 = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim_v1 is not None and claim_v1.attempts == 1

            # A second call for the same client finishes analysis while
            # worker-1 is still holding claim_v1.
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            await worker._fail_or_retry(
                claim_v1, error_code="network_error", error_message="boom", retryable=True
            )

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                # schedule_retry() itself doesn't know about versions -- it
                # just requeues. The reset to a fresh budget only happens
                # once the row is actually reclaimed, below.
                assert job["status"] == "PENDING"
                assert job["attempts"] == 1
                assert job["last_error_code"] == "network_error"

            async with engine.begin() as connection:
                claim_v2_first = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim_v2_first is not None
                assert claim_v2_first.attempts == 1

            await worker._fail_or_retry(
                claim_v2_first, error_code="network_error", error_message="boom", retryable=True
            )

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "PENDING"
                assert job["attempts"] == 1

            async with engine.begin() as connection:
                claim_v2_second = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim_v2_second is not None
                assert claim_v2_second.attempts == 2

            await worker._fail_or_retry(
                claim_v2_second, error_code="network_error", error_message="boom", retryable=True
            )

            async with engine.connect() as connection:
                job = await client_profile_job_row(connection, client_id)
                assert job["status"] == "FAILED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_forever_recovers_a_stale_claim_from_a_crashed_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")
                crashed = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="crashed"
                )
                assert crashed is not None

            analyzer = FakeProfileAnalyzer(
                AggregateProfileResult(profile=CustomerProfile(data={"theme_summary": "x"}))
            )
            worker = ClientProfileWorker(
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
                    job = await client_profile_job_row(connection, client_id)
                    return job["status"] == "COMPLETED"

            await wait_until(is_completed)
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())
