import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fina.config import Settings
from fina.main import create_app
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.runtime import RuntimeState
from fina.services.calls_discovery import PostgresCallsDiscoveryScheduler
from tests.conftest import TEST_MCP_API_KEY, application_client
from tests.integration.helpers import START
from tests.test_calls_discovery import FixedClock

pytestmark = pytest.mark.integration


async def wait_until_blocked(connection: AsyncConnection, blocked: int, blocker: int) -> None:
    async def observe() -> None:
        while True:
            result = await connection.execute(
                text("SELECT :blocker = ANY(pg_blocking_pids(:blocked))"),
                {"blocker": blocker, "blocked": blocked},
            )
            if result.scalar_one():
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(observe(), timeout=5)


def test_two_connections_serialize_discovery_scheduling(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with (
                engine.connect() as first,
                engine.connect() as second,
                engine.connect() as observer,
            ):
                first_pid = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                second_pid = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                first_repo = DiscoveryRunRepository(first)
                second_repo = DiscoveryRunRepository(second)
                run_id = await first_repo.enqueue_next(
                    initial_window_from=START,
                    window_to=START + timedelta(hours=1),
                )
                assert run_id is not None
                waiting = asyncio.create_task(
                    second_repo.enqueue_next(
                        initial_window_from=START + timedelta(minutes=30),
                        window_to=START + timedelta(hours=2),
                    )
                )
                try:
                    await wait_until_blocked(observer, second_pid, first_pid)
                    await first.commit()
                    assert await asyncio.wait_for(waiting, timeout=5) is None
                    await second.commit()
                finally:
                    if not waiting.done():
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                assert (
                    await observer.execute(text("SELECT count(*) FROM discovery_runs"))
                ).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_failure_restart_and_completed_boundaries(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                assert await repo.enqueue_next(initial_window_from=START, window_to=START) is None
                first_id = await repo.enqueue_next(
                    initial_window_from=START, window_to=START + timedelta(hours=1)
                )
                first = await repo.claim_next(worker_id="instance-a")
                assert first is not None and first.id == first_id
                assert await repo.mark_failed(
                    run_id=first.id,
                    worker_id="instance-a",
                    claim_token=first.claim_token,
                    error_code="FAILED",
                    error_message="first discovery failed",
                )
            # A restarted/different replica supplies a later local startup time.
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                retry_id = await repo.enqueue_next(
                    initial_window_from=START + timedelta(hours=2),
                    window_to=START + timedelta(hours=3),
                )
                retry = await repo.claim_next(worker_id="instance-b")
                assert retry is not None and retry.id == retry_id
                assert retry.window_from == START
                assert (
                    await repo.enqueue_next(
                        initial_window_from=START, window_to=START + timedelta(hours=4)
                    )
                    is None
                )
                assert await repo.mark_completed(
                    run_id=retry.id, worker_id="instance-b", claim_token=retry.claim_token
                )
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                assert (
                    await repo.enqueue_next(
                        initial_window_from=START, window_to=START + timedelta(hours=4)
                    )
                    is not None
                )
                next_run = await repo.claim_next(worker_id="instance-a")
                assert next_run is not None
                assert next_run.window_from == START + timedelta(hours=3)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_failed_identical_window_can_be_retried(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                args = dict(initial_window_from=START, window_to=START + timedelta(hours=1))
                run_id = await repo.enqueue_next(**args)
                claim = await repo.claim_next(worker_id="worker")
                assert claim is not None
                assert await repo.mark_failed(
                    run_id=claim.id,
                    worker_id="worker",
                    claim_token=claim.claim_token,
                    error_code="failed",
                    error_message="failed",
                )
                assert await repo.enqueue_next(**args) == run_id
                assert (
                    await connection.execute(text("SELECT count(*) FROM discovery_runs"))
                ).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_earlier_start_backfills_gap_then_resumes_forward(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            existing_from = START + timedelta(days=120)
            existing_to = START + timedelta(days=365)

            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                run_id = await repo.enqueue_next(
                    initial_window_from=existing_from, window_to=existing_to
                )
                assert run_id is not None
                claim = await repo.claim_next(worker_id="worker")
                assert claim is not None and claim.id == run_id
                assert await repo.mark_completed(
                    run_id=claim.id, worker_id="worker", claim_token=claim.claim_token
                )

            # DISCOVERY_START_AT reaches earlier than any recorded window: backfill
            # closes exactly the gap, ignoring the trigger's own window_to.
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                backfill_id = await repo.enqueue_next(
                    initial_window_from=START,
                    window_to=START + timedelta(days=999),
                    backfill_from=START,
                )
                assert backfill_id is not None
                backfill = await repo.claim_next(worker_id="worker")
                assert backfill is not None and backfill.id == backfill_id
                assert backfill.window_from == START
                assert backfill.window_to == existing_from
                assert await repo.mark_completed(
                    run_id=backfill.id, worker_id="worker", claim_token=backfill.claim_token
                )

            # The gap is closed: scheduling resumes forward from the latest
            # completed run, ignoring the now-satisfied DISCOVERY_START_AT.
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                resumed_id = await repo.enqueue_next(
                    initial_window_from=START,
                    window_to=existing_to + timedelta(hours=1),
                    backfill_from=START,
                )
                assert resumed_id is not None
                resumed = await repo.claim_next(worker_id="worker")
                assert resumed is not None and resumed.id == resumed_id
                assert resumed.window_from == existing_to
                assert resumed.window_to == existing_to + timedelta(hours=1)

            async with engine.connect() as connection:
                count = (
                    await connection.execute(text("SELECT count(*) FROM discovery_runs"))
                ).scalar_one()
                assert count == 3
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("completed", [False, True])
@pytest.mark.parametrize("configured_start", [False, True])
def test_earlier_replica_start_does_not_request_backfill(
    database_url: str, completed: bool, configured_start: bool
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            stored_from = START + timedelta(hours=1)
            stored_to = START + timedelta(hours=2)
            now = START + timedelta(hours=3)
            async with engine.begin() as connection:
                jobs = DiscoveryRunRepository(connection)
                await jobs.enqueue_next(initial_window_from=stored_from, window_to=stored_to)
                claim = await jobs.claim_next(worker_id="later-replica")
                assert claim is not None
                if completed:
                    assert await jobs.mark_completed(
                        run_id=claim.id, worker_id="later-replica", claim_token=claim.claim_token
                    )
                else:
                    assert await jobs.mark_failed(
                        run_id=claim.id,
                        worker_id="later-replica",
                        claim_token=claim.claim_token,
                        error_code="TRANSIENT",
                        error_message="temporary failure",
                    )

            async with engine.connect() as connection:
                scheduler = PostgresCallsDiscoveryScheduler(
                    connection=connection,
                    discovery_runs=DiscoveryRunRepository(connection),
                    settings=Settings(
                        database_url=database_url,
                        mcp_api_key=TEST_MCP_API_KEY,
                        discovery_start_at=START if configured_start else None,
                    ),
                    runtime=RuntimeState(started=True, started_at=START),
                    clock=FixedClock(now),
                )
                assert await scheduler.schedule()

            async with engine.connect() as connection:
                pending = (
                    await connection.execute(
                        text(
                            "SELECT window_from, window_to FROM discovery_runs "
                            "WHERE status = 'PENDING'"
                        )
                    )
                ).one()
                if configured_start:
                    assert pending.window_from == START
                    assert pending.window_to == stored_from
                else:
                    assert pending.window_from == (stored_to if completed else stored_from)
                    assert pending.window_to == now
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_discovery_claims_reject_obsolete_tokens(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                await repo.enqueue_next(
                    initial_window_from=START, window_to=START + timedelta(hours=1)
                )
                old = await repo.claim_next(worker_id="same-worker")
                assert old is not None
                locked_at = (
                    await connection.execute(text("SELECT locked_at FROM discovery_runs"))
                ).scalar_one()
                assert await repo.recover_stale(locked_before=locked_at) == []
                assert await repo.recover_stale(locked_before=locked_at + timedelta(seconds=1)) == [
                    old.id
                ]
                new = await repo.claim_next(worker_id="same-worker")
                assert new is not None and new.claim_token != old.claim_token
                assert not await repo.mark_completed(
                    run_id=old.id, worker_id="same-worker", claim_token=old.claim_token
                )
                assert not await repo.mark_failed(
                    run_id=old.id,
                    worker_id="same-worker",
                    claim_token=old.claim_token,
                    error_code="late",
                    error_message="obsolete failure",
                )
                assert not await repo.mark_completed(
                    run_id=new.id, worker_id="other-worker", claim_token=new.claim_token
                )
                assert await repo.mark_completed(
                    run_id=new.id, worker_id="same-worker", claim_token=new.claim_token
                )
                assert await repo.claim_next(worker_id="worker") is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_database_rejects_multiple_active_discovery_runs(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                repo = DiscoveryRunRepository(connection)
                await repo.create_or_get(window_from=START, window_to=START + timedelta(hours=1))
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await DiscoveryRunRepository(connection).create_or_get(
                        window_from=START + timedelta(hours=1),
                        window_to=START + timedelta(hours=2),
                    )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_two_application_instances_share_discovery_state(database_url: str) -> None:
    async def scenario() -> None:
        settings = Settings(
            database_url=database_url, mcp_api_key=TEST_MCP_API_KEY, discovery_start_at=START
        )
        first_app = create_app(settings=settings, clock=FixedClock(START + timedelta(hours=1)))
        second_app = create_app(settings=settings, clock=FixedClock(START + timedelta(hours=2)))
        async with application_client(first_app) as first, application_client(second_app) as second:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    first.post("/internal/tasks/discovery"),
                    second.post("/internal/tasks/discovery"),
                ),
                timeout=5,
            )
            assert [r.status_code for r in responses] == [204, 204]
            for client in [first, second]:
                assert (await client.get("/probes/ready")).status_code == 204
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                row = (
                    await connection.execute(text("SELECT window_from, status FROM discovery_runs"))
                ).one()
                assert row.window_from == START
                assert row.status == "PENDING"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
