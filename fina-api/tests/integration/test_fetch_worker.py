import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.config import Settings
from fina.domain.audio_fetch import (
    CallIdentity,
    FetchCallError,
    FetchCallInput,
    FetchCallResult,
)
from fina.domain.clock import SystemClock
from fina.domain.enums import CallDirection
from fina.main import create_app
from fina.repositories.fetch_jobs import FetchJobRepository
from fina.services.audio_completion import FetchCompletion
from fina.services.fetch_worker import FetchWorker
from tests.conftest import TEST_MCP_API_KEY, application_client
from tests.integration.helpers import ADVISOR, CLIENT, START, seed_call
from tests.test_calls_discovery import FixedClock

pytestmark = pytest.mark.integration

NOW = START + timedelta(days=1)


class FakeAudioFetcher:
    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = dict(responses)
        self.requested: list[str] = []

    async def fetch_call(self, input_data: FetchCallInput):
        self.requested.append(input_data.source_call_id)
        outcome = self._responses[input_data.source_call_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingAudioFetcher:
    """Blocks inside fetch_call until released, to exercise mid-flight shutdown."""

    def __init__(self, outcome: object, *, reached: asyncio.Event, release: asyncio.Event) -> None:
        self._outcome = outcome
        self._reached = reached
        self._release = release

    async def fetch_call(self, input_data: FetchCallInput):
        self._reached.set()
        await self._release.wait()
        return self._outcome


def expected_identity(source_id: str) -> CallIdentity:
    return CallIdentity(
        source_call_id=source_id,
        started_at=START,
        advisor_phone=ADVISOR,
        counterparty_phone=CLIENT,
        call_direction=CallDirection.INBOUND,
    )


async def fetch_job_row(connection, call_id) -> dict:
    return (
        (
            await connection.execute(
                text("SELECT * FROM audio_fetch_jobs WHERE call_id = :call_id"),
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


def test_successful_fetch_completes_job_and_enqueues_analysis(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallResult(
                        identity=expected_identity("call-1"),
                        manifest_object_key="audio/call-1.json",
                    )
                }
            )
            worker = FetchWorker(
                engine=engine, fetcher=fetcher, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                assert job["status"] == "COMPLETED"
                assert job["object_key"] == "audio/call-1.json"
                assert job["claim_token"] is None and job["locked_by"] is None
                analysis = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, object_key FROM audio_analysis_jobs "
                                "WHERE call_id = :id"
                            ),
                            {"id": call_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert analysis["status"] == "PENDING"
                assert analysis["object_key"] == "audio/call-1.json"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("new_claim_completed", [False, True])
@pytest.mark.parametrize("new_worker_id", ["worker-1", "worker-2"])
def test_fenced_out_completion_cannot_store_manifest_or_enqueue_analysis(
    database_url: str,
    new_claim_completed: bool,
    new_worker_id: str,
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="fina.services.fetch_worker")

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                stale_claim = await FetchJobRepository(connection).claim_next(worker_id="worker-1")
                assert stale_claim is not None

            completion = FetchCompletion(
                claim=stale_claim,
                result=FetchCallResult(
                    identity=expected_identity("call-1"), manifest_object_key="audio/stale.json"
                ),
            )

            # A matching result does not guarantee that its claim is still owned.
            async with engine.begin() as connection:
                await FetchJobRepository(connection).recover_stale(
                    locked_before=datetime.now(UTC) + timedelta(minutes=1)
                )
                real_claim = await FetchJobRepository(connection).claim_next(
                    worker_id=new_worker_id
                )
                assert real_claim is not None and real_claim.call_id == call_id
                assert real_claim.claim_token != stale_claim.claim_token

            if new_claim_completed:
                new_worker = FetchWorker(
                    engine=engine,
                    fetcher=FakeAudioFetcher({}),
                    clock=FixedClock(NOW),
                    worker_id=new_worker_id,
                )
                await new_worker._complete(
                    FetchCompletion(
                        claim=real_claim,
                        result=FetchCallResult(
                            identity=expected_identity("call-1"),
                            manifest_object_key="audio/current.json",
                        ),
                    )
                )

            worker = FetchWorker(
                engine=engine,
                fetcher=FakeAudioFetcher({}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )
            caplog.clear()
            await worker._complete(completion)
            records = [r for r in caplog.records if r.name == "fina.services.fetch_worker"]
            assert len(records) == 1
            assert records[0].levelno == logging.WARNING
            assert records[0].getMessage() == "job update skipped for a lost or superseded claim"
            assert records[0].call_id == str(call_id)
            assert records[0].claim_token == str(stale_claim.claim_token)

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                if new_claim_completed:
                    assert job["status"] == "COMPLETED"
                    assert job["object_key"] == "audio/current.json"
                else:
                    assert job["status"] == "IN_PROGRESS"
                    assert job["claim_token"] == real_claim.claim_token
                    assert job["object_key"] is None
                analysis_keys = (
                    (
                        await connection.execute(
                            text("SELECT object_key FROM audio_analysis_jobs WHERE call_id = :id"),
                            {"id": call_id},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert list(analysis_keys) == (
                    ["audio/current.json"] if new_claim_completed else []
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retryable_failure_schedules_immediate_retry(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallError(
                        error_code="TIMEOUT", error_message="timed out", retryable=True
                    )
                }
            )
            worker = FetchWorker(
                engine=engine, fetcher=fetcher, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                assert job["status"] == "PENDING"
                assert job["attempts"] == 1
                assert job["last_error_code"] == "TIMEOUT"
                assert job["claim_token"] is None and job["locked_by"] is None
                # No backoff delay: immediately eligible for the next claim.
                assert job["available_at"] == NOW
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_non_retryable_failure_marks_job_failed(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallError(
                        error_code="NOT_FOUND", error_message="unknown call", retryable=False
                    )
                }
            )
            worker = FetchWorker(
                engine=engine, fetcher=fetcher, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "NOT_FOUND"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_exhausted_attempts_marks_failed_even_if_retryable(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallError(
                        error_code="TIMEOUT", error_message="timed out", retryable=True
                    )
                }
            )
            worker = FetchWorker(
                engine=engine,
                fetcher=fetcher,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                max_attempts=1,
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                # attempts is 1 on the first claim, which is not < max_attempts=1.
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "TIMEOUT"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_identity_mismatch_marks_job_failed_without_retry(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            wrong_identity = expected_identity("call-1").model_copy(
                update={"advisor_phone": "79990009999"}
            )
            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallResult(
                        identity=wrong_identity, manifest_object_key="audio/call-1.json"
                    )
                }
            )
            worker = FetchWorker(
                engine=engine, fetcher=fetcher, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "IDENTITY_MISMATCH"
                assert job["object_key"] is None
                analysis_count = (
                    await connection.execute(
                        text("SELECT count(*) FROM audio_analysis_jobs WHERE call_id = :id"),
                        {"id": call_id},
                    )
                ).scalar_one()
                assert analysis_count == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unexpected_exception_is_retried(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher({"call-1": RuntimeError("boom")})
            worker = FetchWorker(
                engine=engine, fetcher=fetcher, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                assert job["status"] == "PENDING"
                assert job["last_error_code"] == "FETCH_UNEXPECTED_ERROR"
                assert "boom" in job["last_error_message"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_once_returns_false_when_no_jobs_available(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            worker = FetchWorker(
                engine=engine,
                fetcher=FakeAudioFetcher({}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )
            assert await worker.run_once() is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_two_workers_do_not_double_process_same_job(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await seed_call(connection, "call-1")
                await seed_call(connection, "call-2")

            responses = {
                "call-1": FetchCallResult(
                    identity=expected_identity("call-1"), manifest_object_key="audio/call-1.json"
                ),
                "call-2": FetchCallResult(
                    identity=expected_identity("call-2"), manifest_object_key="audio/call-2.json"
                ),
            }
            fetcher_a = FakeAudioFetcher(responses)
            fetcher_b = FakeAudioFetcher(responses)
            worker_a = FetchWorker(
                engine=engine, fetcher=fetcher_a, clock=FixedClock(NOW), worker_id="instance-a"
            )
            worker_b = FetchWorker(
                engine=engine, fetcher=fetcher_b, clock=FixedClock(NOW), worker_id="instance-b"
            )

            results = await asyncio.gather(worker_a.run_once(), worker_b.run_once())

            assert results == [True, True]
            assert sorted(fetcher_a.requested + fetcher_b.requested) == ["call-1", "call-2"]
            async with engine.connect() as connection:
                statuses = (
                    (await connection.execute(text("SELECT status FROM audio_fetch_jobs")))
                    .scalars()
                    .all()
                )
                assert list(statuses) == ["COMPLETED", "COMPLETED"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_forever_processes_then_stops_promptly_on_signal(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallResult(
                        identity=expected_identity("call-1"),
                        manifest_object_key="audio/call-1.json",
                    )
                }
            )
            worker = FetchWorker(
                engine=engine,
                fetcher=fetcher,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                poll_interval=timedelta(milliseconds=20),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await fetch_job_row(connection, call_id)
                    return job["status"] == "COMPLETED"

            await wait_until(is_completed)

            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
            assert task.done() and task.exception() is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_shutdown_lets_an_in_flight_job_finish(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            reached = asyncio.Event()
            release = asyncio.Event()
            fetcher = BlockingAudioFetcher(
                FetchCallResult(
                    identity=expected_identity("call-1"), manifest_object_key="audio/call-1.json"
                ),
                reached=reached,
                release=release,
            )
            worker = FetchWorker(
                engine=engine,
                fetcher=fetcher,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                poll_interval=timedelta(milliseconds=20),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            await asyncio.wait_for(reached.wait(), timeout=2)
            stop_event.set()
            await asyncio.sleep(0.1)
            assert not task.done(), "shutdown must not abandon a job already being fetched"

            release.set()
            await asyncio.wait_for(task, timeout=2)

            async with engine.connect() as connection:
                job = await fetch_job_row(connection, call_id)
                assert job["status"] == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_forever_recovers_a_stale_claim_from_a_crashed_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                # Simulate a worker that claimed the job and then crashed.
                crashed = await FetchJobRepository(connection).claim_next(worker_id="crashed")
                assert crashed is not None

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallResult(
                        identity=expected_identity("call-1"),
                        manifest_object_key="audio/call-1.json",
                    )
                }
            )
            # locked_at is set by the database's own now(), so recovery must be
            # judged against real wall-clock time, not the fictional FixedClock
            # used elsewhere to make available_at assertions deterministic.
            worker = FetchWorker(
                engine=engine,
                fetcher=fetcher,
                clock=SystemClock(),
                worker_id="worker-1",
                poll_interval=timedelta(milliseconds=20),
                stale_after=timedelta(seconds=0),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await fetch_job_row(connection, call_id)
                    return job["status"] == "COMPLETED"

            await wait_until(is_completed)
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_recover_stale_logs_the_recovered_call_ids(database_url: str, caplog) -> None:
    """Stale-claim recovery must log which jobs were recovered, not just a
    count, so an operator can trace a specific job's history."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                crashed = await FetchJobRepository(connection).claim_next(worker_id="crashed")
                assert crashed is not None

            worker = FetchWorker(
                engine=engine,
                fetcher=FakeAudioFetcher({}),
                clock=SystemClock(),
                worker_id="worker-1",
                stale_after=timedelta(seconds=0),
            )
            with caplog.at_level("WARNING"):
                await worker._recover_stale()

            [record] = [
                r for r in caplog.records if "recovered stale fetch job claims" in r.message
            ]
            assert record.count == 1
            assert record.call_ids == [str(call_id)]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_app_starts_and_stops_the_fetch_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            fetcher = FakeAudioFetcher(
                {
                    "call-1": FetchCallResult(
                        identity=expected_identity("call-1"),
                        manifest_object_key="audio/call-1.json",
                    )
                }
            )
            settings = Settings(database_url=database_url, mcp_api_key=TEST_MCP_API_KEY)
            application = create_app(settings=settings, audio_fetcher=fetcher)

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await fetch_job_row(connection, call_id)
                    return job["status"] == "COMPLETED"

            async with application_client(application):
                # The worker starts as part of the app's own lifespan, unprompted.
                await wait_until(is_completed)
            # Exiting the context tears the worker down; reaching here without
            # hanging or raising is itself proof shutdown completed cleanly.
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_app_without_a_fetcher_runs_no_worker(database_url: str) -> None:
    async def scenario() -> None:
        settings = Settings(database_url=database_url, mcp_api_key=TEST_MCP_API_KEY)
        application = create_app(settings=settings)
        async with application_client(application) as client:
            assert (await client.get("/probes/ready")).status_code == 204

    asyncio.run(scenario())
