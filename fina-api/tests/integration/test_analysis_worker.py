import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.config import Settings
from fina.domain.audio_analysis import AnalyzeError, AnalyzeInput, AnalyzeResult
from fina.domain.audio_contracts import CallIdentity
from fina.domain.audio_fetch import FetchCallInput, FetchCallResult
from fina.domain.clock import SystemClock
from fina.domain.enums import CallDirection
from fina.main import create_app
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.services.analysis_indexing import build_search_text
from fina.services.analysis_worker import AnalysisWorker
from fina.services.audio_completion import AnalysisCompletion
from tests.analysis_examples import analysis_data
from tests.conftest import TEST_MCP_API_KEY, application_client
from tests.integration.helpers import ADVISOR, CLIENT, START, enqueue_analysis, seed_call
from tests.test_calls_discovery import FixedClock

pytestmark = pytest.mark.integration

NOW = START + timedelta(days=1)
SOURCE_ID = "source-123"


class FakeAudioAnalyzer:
    def __init__(self, responses: dict[UUID, object]) -> None:
        self._responses = dict(responses)
        self.requested: list[UUID] = []

    async def analyze(self, input_data: AnalyzeInput):
        self.requested.append(input_data.task_id)
        outcome = self._responses[input_data.task_id]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingAudioAnalyzer:
    """Blocks inside analyze until released, to exercise mid-flight shutdown."""

    def __init__(self, outcome: object, *, reached: asyncio.Event, release: asyncio.Event) -> None:
        self._outcome = outcome
        self._reached = reached
        self._release = release

    async def analyze(self, input_data: AnalyzeInput):
        self._reached.set()
        await self._release.wait()
        return self._outcome


async def seed_analysis_job(connection, source_id: str = SOURCE_ID) -> UUID:
    call_id = await seed_call(connection, source_id)
    await enqueue_analysis(connection, call_id)
    return call_id


def analysis_result_for(call_id: UUID) -> AnalyzeResult:
    return AnalyzeResult.model_validate(analysis_data(call_id))


async def analysis_job_row(connection, call_id) -> dict:
    return (
        (
            await connection.execute(
                text("SELECT * FROM audio_analysis_jobs WHERE call_id = :call_id"),
                {"call_id": call_id},
            )
        )
        .mappings()
        .one()
    )


async def client_profile_job_status(connection, call_id: UUID) -> str | None:
    return (
        await connection.execute(
            text(
                "SELECT job.status FROM client_profile_jobs AS job "
                "JOIN calls AS call ON call.client_id = job.client_id "
                "WHERE call.id = :call_id"
            ),
            {"call_id": call_id},
        )
    ).scalar_one_or_none()


async def send_order_job_row(connection, call_id: UUID) -> dict | None:
    return (
        (
            await connection.execute(
                text("SELECT * FROM send_order_jobs WHERE call_id = :call_id"),
                {"call_id": call_id},
            )
        )
        .mappings()
        .one_or_none()
    )


async def wait_until(condition, *, timeout: float = 5) -> None:
    async def poll() -> None:
        while not await condition():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


def test_successful_analysis_completes_job_and_indexes_search_text(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            result = AnalyzeResult.model_validate(analysis_data(call_id))
            analyzer = FakeAudioAnalyzer({call_id: result})
            worker = AnalysisWorker(
                engine=engine, analyzer=analyzer, clock=FixedClock(NOW), worker_id="worker-1"
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "COMPLETED"
                assert job["analysis_result"]["provider_result"] == result.provider_result
                assert job["claim_token"] is None and job["locked_by"] is None
                indexed = (
                    (
                        await connection.execute(
                            text(
                                "SELECT orders_search @@ plainto_tsquery('russian', 'Сбербанк') "
                                "AS order_matches FROM call_search WHERE call_id = :id"
                            ),
                            {"id": call_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert indexed["order_matches"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_successful_analysis_enqueues_client_profile_job(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            result = AnalyzeResult.model_validate(analysis_data(call_id))
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: result}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                assert await client_profile_job_status(connection, call_id) == "PENDING"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_successful_analysis_with_pre_order_enqueues_send_order_job(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            # analysis_data() bakes in a pre_order (buying Сбербанк).
            result = AnalyzeResult.model_validate(analysis_data(call_id))
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: result}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await send_order_job_row(connection, call_id)
                assert job is not None
                assert job["status"] == "PENDING"
                assert job["client_phone"] == CLIENT
                assert job["pre_order"]["instrument_name"] is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_retryable_failure_schedules_immediate_retry(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            error = AnalyzeError(
                task_id=call_id, error_code="TIMEOUT", error_message="timed out", retryable=True
            )
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: error}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "PENDING"
                assert job["attempts"] == 1
                assert job["last_error_code"] == "TIMEOUT"
                # No backoff delay: immediately eligible for the next claim.
                assert job["available_at"] == NOW
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_exhausted_attempts_marks_failed_even_if_retryable(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            error = AnalyzeError(
                task_id=call_id, error_code="TIMEOUT", error_message="timed out", retryable=True
            )
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: error}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
                max_attempts=1,
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "FAILED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_non_retryable_failure_marks_job_failed(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            error = AnalyzeError(
                task_id=call_id,
                error_code="UNSUPPORTED",
                error_message="bad audio",
                retryable=False,
            )
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: error}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "UNSUPPORTED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome_kind", ["success", "failure"])
def test_task_id_mismatch_marks_job_failed_without_retry(
    database_url: str, outcome_kind: str
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            wrong_task_id = uuid4()
            outcome = (
                AnalyzeResult.model_validate(analysis_data(wrong_task_id))
                if outcome_kind == "success"
                else AnalyzeError(
                    task_id=wrong_task_id,
                    error_code="TIMEOUT",
                    error_message="timed out",
                    retryable=True,
                )
            )
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: outcome}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "FAILED"
                assert job["last_error_code"] == "TASK_ID_MISMATCH"
                assert job["analysis_result"] is None
                assert await client_profile_job_status(connection, call_id) is None
                assert await send_order_job_row(connection, call_id) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_unexpected_exception_is_retried(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: RuntimeError("boom")}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )

            assert await worker.run_once() is True

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "PENDING"
                assert job["last_error_code"] == "ANALYZE_UNEXPECTED_ERROR"
                assert "boom" in job["last_error_message"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_once_returns_false_when_no_jobs_available(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({}),
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
                call_id_1 = await seed_analysis_job(connection, "source-1")
                call_id_2 = await seed_call(connection, "source-2")
                await enqueue_analysis(connection, call_id_2)

            responses = {
                call_id_1: analysis_result_for(call_id_1),
                call_id_2: analysis_result_for(call_id_2),
            }
            analyzer_a = FakeAudioAnalyzer(responses)
            analyzer_b = FakeAudioAnalyzer(responses)
            worker_a = AnalysisWorker(
                engine=engine, analyzer=analyzer_a, clock=FixedClock(NOW), worker_id="instance-a"
            )
            worker_b = AnalysisWorker(
                engine=engine, analyzer=analyzer_b, clock=FixedClock(NOW), worker_id="instance-b"
            )

            results = await asyncio.gather(worker_a.run_once(), worker_b.run_once())

            assert results == [True, True]
            assert sorted(analyzer_a.requested + analyzer_b.requested) == sorted(
                [call_id_1, call_id_2]
            )
            async with engine.connect() as connection:
                statuses = (
                    (await connection.execute(text("SELECT status FROM audio_analysis_jobs")))
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
                call_id = await seed_analysis_job(connection)

            result = AnalyzeResult.model_validate(analysis_data(call_id))
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: result}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
                poll_interval=timedelta(milliseconds=20),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await analysis_job_row(connection, call_id)
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
                call_id = await seed_analysis_job(connection)

            reached = asyncio.Event()
            release = asyncio.Event()
            result = AnalyzeResult.model_validate(analysis_data(call_id))
            analyzer = BlockingAudioAnalyzer(result, reached=reached, release=release)
            worker = AnalysisWorker(
                engine=engine,
                analyzer=analyzer,
                clock=FixedClock(NOW),
                worker_id="worker-1",
                poll_interval=timedelta(milliseconds=20),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            await asyncio.wait_for(reached.wait(), timeout=2)
            stop_event.set()
            await asyncio.sleep(0.1)
            assert not task.done(), "shutdown must not abandon a job already being analyzed"

            release.set()
            await asyncio.wait_for(task, timeout=2)

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                assert job["status"] == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_run_forever_recovers_a_stale_claim_from_a_crashed_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)
                # Simulate a worker that claimed the job and then crashed.
                crashed = await AnalysisJobRepository(connection).claim_next(worker_id="crashed")
                assert crashed is not None

            result = AnalyzeResult.model_validate(analysis_data(call_id))
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({call_id: result}),
                clock=SystemClock(),
                worker_id="worker-1",
                poll_interval=timedelta(milliseconds=20),
                stale_after=timedelta(seconds=0),
            )
            stop_event = asyncio.Event()
            task = asyncio.create_task(worker.run_forever(stop_event))

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await analysis_job_row(connection, call_id)
                    return job["status"] == "COMPLETED"

            await wait_until(is_completed)
            stop_event.set()
            await asyncio.wait_for(task, timeout=2)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_app_starts_and_stops_the_analysis_worker(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)

            result = AnalyzeResult.model_validate(analysis_data(call_id))
            analyzer = FakeAudioAnalyzer({call_id: result})
            settings = Settings(database_url=database_url, mcp_api_key=TEST_MCP_API_KEY)
            application = create_app(settings=settings, audio_analyzer=analyzer)

            async def is_completed() -> bool:
                async with engine.connect() as connection:
                    job = await analysis_job_row(connection, call_id)
                    return job["status"] == "COMPLETED"

            async with application_client(application):
                await wait_until(is_completed)
            # Reaching here without hanging or raising proves shutdown completed.
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_app_runs_both_workers_independently(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            # analysis-only's own fetch+enqueue must complete before fetch-only
            # exists, or enqueue_analysis's claim_next (which just grabs
            # whichever fetch job is next) could pick up the wrong one.
            async with engine.begin() as connection:
                analysis_call_id = await seed_analysis_job(connection, "analysis-only")
            async with engine.begin() as connection:
                fetch_call_id = await seed_call(connection, "fetch-only")

            class FetchFake:
                async def fetch_call(self, input_data: FetchCallInput):
                    return FetchCallResult(
                        identity=CallIdentity(
                            source_call_id=input_data.source_call_id,
                            started_at=START,
                            advisor_phone=ADVISOR,
                            counterparty_phone=CLIENT,
                            call_direction=CallDirection.INBOUND,
                        ),
                        manifest_object_key="audio/fetch-only.json",
                    )

            analysis_result = analysis_result_for(analysis_call_id)
            settings = Settings(database_url=database_url, mcp_api_key=TEST_MCP_API_KEY)
            application = create_app(
                settings=settings,
                audio_fetcher=FetchFake(),
                audio_analyzer=FakeAudioAnalyzer({analysis_call_id: analysis_result}),
            )

            async def both_done() -> bool:
                async with engine.connect() as connection:
                    fetch_status = (
                        await connection.execute(
                            text("SELECT status FROM audio_fetch_jobs WHERE call_id = :id"),
                            {"id": fetch_call_id},
                        )
                    ).scalar_one()
                    analysis_status = (
                        await connection.execute(
                            text("SELECT status FROM audio_analysis_jobs WHERE call_id = :id"),
                            {"id": analysis_call_id},
                        )
                    ).scalar_one()
                    return fetch_status == "COMPLETED" and analysis_status == "COMPLETED"

            async with application_client(application):
                await wait_until(both_done)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("new_claim_completed", [False, True])
@pytest.mark.parametrize("new_worker_id", ["worker-1", "worker-2"])
def test_fenced_out_completion_creates_no_order_or_send_order_work(
    database_url: str, new_claim_completed: bool, new_worker_id: str
) -> None:
    """If a worker's claim is recovered as stale and reclaimed while it's
    still (slowly) processing, its eventual _complete() call must be a no-op:
    complete_and_index()'s own fencing already prevents it from overwriting
    the analysis result, but the order/client-profile/send-order side effects
    must not fire either."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_analysis_job(connection)
                stale_claim = await AnalysisJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert stale_claim is not None

            stale_result = AnalyzeResult.model_validate(analysis_data(call_id))
            assert stale_result.artifacts.pre_order is not None
            completion = AnalysisCompletion(claim=stale_claim, result=stale_result)

            # Ownership can change after construction. Reusing the worker ID
            # must still fence out the old token, even before the new job finishes.
            async with engine.begin() as connection:
                await AnalysisJobRepository(connection).recover_stale(
                    locked_before=datetime.now(UTC) + timedelta(minutes=1)
                )
                real_claim = await AnalysisJobRepository(connection).claim_next(
                    worker_id=new_worker_id
                )
                assert real_claim is not None and real_claim.call_id == call_id
                assert real_claim.claim_token != stale_claim.claim_token
                real_data = analysis_data(call_id)
                del real_data["artifacts"]["pre_order"]
                real_result = AnalyzeResult.model_validate(real_data)
                if new_claim_completed:
                    assert await AnalysisJobRepository(connection).complete_and_index(
                        call_id=call_id,
                        worker_id=new_worker_id,
                        claim_token=real_claim.claim_token,
                        analysis_result=real_result.to_storage(),
                        analysis_schema_version=real_result.schema_version,
                        **build_search_text(real_result.artifacts).model_dump(),
                    )

            # worker-1 now finally finishes its own (stale) analysis, which
            # does have a pre_order -- its completion must be entirely ignored.
            worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer({}),
                clock=FixedClock(NOW),
                worker_id="worker-1",
            )
            await worker._complete(completion)

            async with engine.connect() as connection:
                job = await analysis_job_row(connection, call_id)
                if new_claim_completed:
                    assert job["status"] == "COMPLETED"
                    assert job["analysis_result"] == real_result.to_storage()
                else:
                    assert job["status"] == "IN_PROGRESS"
                    assert job["claim_token"] == real_claim.claim_token
                    assert job["analysis_result"] is None
                assert await client_profile_job_status(connection, call_id) is None

                search_count = (
                    await connection.execute(
                        text("SELECT count(*) FROM call_search WHERE call_id = :id"),
                        {"id": call_id},
                    )
                ).scalar_one()
                assert search_count == int(new_claim_completed)

                order_count = (
                    await connection.execute(
                        text("SELECT count(*) FROM orders WHERE call_id = :id"), {"id": call_id}
                    )
                ).scalar_one()
                assert order_count == 0

                send_order_count = (
                    await connection.execute(
                        text("SELECT count(*) FROM send_order_jobs WHERE call_id = :id"),
                        {"id": call_id},
                    )
                ).scalar_one()
                assert send_order_count == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())
