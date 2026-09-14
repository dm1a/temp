import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.audio_analysis import AggregateProfileError
from fina.domain.clock import SystemClock
from fina.repositories.client_profile_jobs import ClientProfileJobRepository
from fina.services.client_profile_worker import ClientProfileWorker
from tests.integration.test_client_profile_worker import (
    FakeProfileAnalyzer,
    seed_completed_analysis,
)

pytestmark = pytest.mark.integration


@asynccontextmanager
async def database_engine(url):
    engine = create_async_engine(
        url, connect_args={"server_settings": {"statement_timeout": "15000"}}
    )
    try:
        yield engine
    finally:
        await engine.dispose()


async def seed(engine):
    async with engine.begin() as connection:
        _, client_id = await seed_completed_analysis(connection, "call-1")
        claim = await ClientProfileJobRepository(connection).claim_next(worker_id="A")
        assert claim is not None
    return client_id, claim


async def state(engine, client_id):
    async with engine.connect() as connection:
        return (
            (
                await connection.execute(
                    text("""
            SELECT job.*, client.customer_profile
            FROM client_profile_jobs job JOIN clients client ON client.id = job.client_id
            WHERE job.client_id = :id
        """),
                    {"id": client_id},
                )
            )
            .mappings()
            .one()
        )


async def finish(connection, claim, worker="A", profile=None):
    return await ClientProfileJobRepository(connection).complete_with_profile(
        client_id=claim.client_id,
        worker_id=worker,
        claim_token=claim.claim_token,
        claimed_request_version=claim.request_version,
        customer_profile=profile or {"version": worker},
    )


def launch(engine, operation):
    ready = asyncio.get_running_loop().create_future()

    async def work():
        async with engine.begin() as connection:
            pid = (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            ready.set_result(pid)
            return await operation(connection)

    return asyncio.create_task(work()), ready


async def wait_for_lock(engine, pid):
    async def poll():
        while True:
            async with engine.connect() as connection:
                blockers = (
                    await connection.execute(
                        text("""
                    SELECT pg_blocking_pids(pid) FROM pg_stat_activity
                    WHERE pid = :pid AND wait_event_type = 'Lock'
                """),
                        {"pid": pid},
                    )
                ).scalar_one_or_none()
            if blockers:
                return blockers
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 5)


async def cancel_pending(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)


def test_completion_holds_claim_lock_until_profile_write_commits(database_url):
    async def scenario():
        async with database_engine(database_url) as engine:
            client_id, claim = await seed(engine)
            publication = recovery = None
            try:
                async with engine.begin() as blocker:
                    await blocker.execute(
                        text("SELECT id FROM clients WHERE id = :id FOR NO KEY UPDATE"),
                        {"id": client_id},
                    )
                    publication, writer_ready = launch(
                        engine, lambda connection: finish(connection, claim)
                    )
                    writer_pid = await asyncio.wait_for(writer_ready, 5)
                    await wait_for_lock(engine, writer_pid)
                    recovery, recovery_ready = launch(
                        engine,
                        lambda connection: ClientProfileJobRepository(connection).recover_stale(
                            locked_before=datetime.now(UTC) + timedelta(minutes=1)
                        ),
                    )
                    recovery_pid = await asyncio.wait_for(recovery_ready, 5)
                    assert writer_pid in await wait_for_lock(engine, recovery_pid)
                    assert not publication.done() and not recovery.done()
                assert await asyncio.wait_for(publication, 5)
                assert await asyncio.wait_for(recovery, 5) == []
                row = await state(engine, client_id)
                assert row["status"] == "COMPLETED"
                assert row["customer_profile"] == {"version": "A"}
            finally:
                await cancel_pending(publication, recovery)

    asyncio.run(scenario())


def test_waiting_old_completion_rechecks_claim_after_reassignment(database_url):
    async def scenario():
        async with database_engine(database_url) as engine:
            client_id, stale_claim = await seed(engine)
            stale_task = None
            try:
                async with engine.begin() as winner:
                    jobs = ClientProfileJobRepository(winner)
                    assert await jobs.recover_stale(
                        locked_before=datetime.now(UTC) + timedelta(minutes=1)
                    ) == [client_id]
                    fresh_claim = await jobs.claim_next(worker_id="B")
                    assert fresh_claim is not None
                    stale_task, ready = launch(
                        engine, lambda connection: finish(connection, stale_claim)
                    )
                    await wait_for_lock(engine, await asyncio.wait_for(ready, 5))
                    assert await finish(winner, fresh_claim, worker="B")
                assert await asyncio.wait_for(stale_task, 5) is False
                row = await state(engine, client_id)
                assert row["customer_profile"] == {"version": "B"}
                assert row["status"] == "COMPLETED"
            finally:
                await cancel_pending(stale_task)

    asyncio.run(scenario())


def test_late_enqueue_from_earlier_transaction_is_preserved(database_url):
    async def scenario():
        async with database_engine(database_url) as engine:
            async with engine.begin() as slow_analysis:
                earlier = (await slow_analysis.execute(text("SELECT now()"))).scalar_one()
                client_id, claim = await seed(engine)
                async with engine.connect() as check:
                    assert earlier < (await check.execute(text("SELECT now()"))).scalar_one()
                await ClientProfileJobRepository(slow_analysis).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                assert await finish(connection, claim) is False
            row = await state(engine, client_id)
            assert row["status"] == "PENDING"
            assert row["request_version"] == claim.request_version + 1
            assert row["customer_profile"] is None
            async with engine.begin() as connection:
                refreshed = await ClientProfileJobRepository(connection).claim_next(worker_id="B")
                assert refreshed is not None
                assert refreshed.request_version == row["request_version"]
                assert refreshed.attempts == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("retryable", [False, True])
def test_new_request_survives_terminal_failure_of_active_run(database_url, retryable):
    async def scenario():
        async with database_engine(database_url) as engine:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            class Analyzer:
                async def aggregate_client_profile(self, **kwargs):
                    async with engine.begin() as connection:
                        await seed_completed_analysis(connection, "call-2")
                    return AggregateProfileError(error_code="network_error", retryable=retryable)

            worker = ClientProfileWorker(
                engine=engine,
                analyzer=Analyzer(),
                clock=SystemClock(),
                worker_id="A",
                max_attempts=1,
            )
            assert await worker.run_once()
            row = await state(engine, client_id)
            assert row["status"] == "PENDING"
            async with engine.begin() as connection:
                refreshed = await ClientProfileJobRepository(connection).claim_next(worker_id="B")
                assert refreshed is not None
                assert refreshed.attempts == 1

    asyncio.run(scenario())


def test_five_successful_refreshes_do_not_consume_next_retry_budget(database_url):
    async def scenario():
        async with database_engine(database_url) as engine:
            client_id, claim = await seed(engine)
            for index in range(5):
                async with engine.begin() as connection:
                    jobs = ClientProfileJobRepository(connection)
                    if index:
                        await jobs.enqueue(client_id=client_id)
                        claim = await jobs.claim_next(worker_id="A")
                    assert claim is not None and claim.attempts == 1
                    assert await finish(connection, claim)
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            worker = ClientProfileWorker(
                engine=engine,
                analyzer=FakeProfileAnalyzer(
                    AggregateProfileError(error_code="network_error", retryable=True)
                ),
                clock=SystemClock(),
                worker_id="A",
                max_attempts=5,
            )
            assert await worker.run_once()
            row = await state(engine, client_id)
            assert row["status"] == "PENDING"
            assert row["attempts"] == 1

    asyncio.run(scenario())


def test_profile_publication_rolls_back_with_job_completion(database_url):
    async def scenario():
        async with database_engine(database_url) as engine:
            client_id, claim = await seed(engine)
            async with engine.connect() as connection:
                transaction = await connection.begin()
                assert await finish(connection, claim)
                await transaction.rollback()
            row = await state(engine, client_id)
            assert row["status"] == "IN_PROGRESS"
            assert row["claim_token"] == claim.claim_token
            assert row["customer_profile"] is None

    asyncio.run(scenario())


@pytest.mark.parametrize("arrival", ["during_active_run", "while_pending_retry"])
def test_new_version_during_retries_gets_a_full_attempt_budget(database_url, arrival):
    async def scenario():
        async with database_engine(database_url) as engine:
            async with engine.begin() as connection:
                _, client_id = await seed_completed_analysis(connection, "call-1")

            class Analyzer:
                calls = 0

                async def aggregate_client_profile(self, **kwargs):
                    self.calls += 1
                    if arrival == "during_active_run" and self.calls == 1:
                        async with engine.begin() as connection:
                            await seed_completed_analysis(connection, "call-2")
                    return AggregateProfileError(error_code="network_error", retryable=True)

            worker = ClientProfileWorker(
                engine=engine,
                analyzer=Analyzer(),
                clock=SystemClock(),
                worker_id="A",
                max_attempts=2,
            )
            assert await worker.run_once()
            if arrival == "while_pending_retry":
                async with engine.begin() as connection:
                    await seed_completed_analysis(connection, "call-2")
            # This is the first analysis attempt using the new input history.
            # Its transient failure should still leave one retry available.
            assert await worker.run_once()
            row = await state(engine, client_id)
            assert row["status"] == "PENDING", dict(row)
            assert row["attempts"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("new_request", [False, True])
def test_recovery_preserves_budget_only_for_the_same_request_version(database_url, new_request):
    async def scenario():
        async with database_engine(database_url) as engine:
            client_id, crashed_claim = await seed(engine)
            assert crashed_claim.attempts == 1
            if new_request:
                async with engine.begin() as connection:
                    await seed_completed_analysis(connection, "call-2")
            # Replica A dies without returning an analyzer result. Replica B
            # must recover the abandoned claim using the normal polling path.
            async with engine.begin() as connection:
                await connection.execute(
                    text("""
                    UPDATE client_profile_jobs
                    SET locked_at = now() - interval '11 minutes'
                    WHERE client_id = :id
                """),
                    {"id": client_id},
                )
            worker = ClientProfileWorker(
                engine=engine,
                analyzer=FakeProfileAnalyzer(
                    AggregateProfileError(error_code="network_error", retryable=True)
                ),
                clock=SystemClock(),
                worker_id="B",
                max_attempts=2,
            )
            assert await worker._iteration(asyncio.Event())
            row = await state(engine, client_id)
            expected = ("PENDING", 1) if new_request else ("FAILED", 2)
            assert (row["status"], row["attempts"]) == expected, dict(row)

    asyncio.run(scenario())
