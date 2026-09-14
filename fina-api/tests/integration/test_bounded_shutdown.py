import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from dishka import make_async_container
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.config import Settings
from fina.di.providers import ApplicationProvider
from fina.domain.audio_fetch import DiscoveredCall, FetchCallInput, FetchCallOutcome
from fina.lifespan import application_lifecycle
from fina.repositories.fetch_jobs import FetchJobRepository
from tests.integration.helpers import seed_call

pytestmark = pytest.mark.integration


class HangingAudioFetcher:
    """fetch_call() never returns -- simulates a worker stuck on a slow or
    hung external call, which a bounded shutdown must not wait out
    indefinitely. list_available_calls() returns immediately so the
    discovery worker (started alongside the fetch worker whenever an
    AudioFetcher is supplied) doesn't also spend the whole test failing
    with AttributeError on every poll."""

    def __init__(self, reached: asyncio.Event) -> None:
        self._reached = reached

    async def fetch_call(self, input_data: FetchCallInput) -> FetchCallOutcome:
        self._reached.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the hang above never returns")

    async def list_available_calls(
        self, *, advisor_phone: str, window_from: datetime, window_to: datetime
    ) -> list[DiscoveredCall]:
        return []


def test_bounded_shutdown_cancels_a_stuck_worker_and_leaves_its_claim_recoverable(
    database_url: str,
) -> None:
    """A worker stuck mid-job (e.g. a hung external call) must not block
    shutdown forever. Once the bounded grace period elapses, its task is
    cancelled -- and cancelling mid-transaction rolls back cleanly (the
    claim row is untouched, still IN_PROGRESS with its original claim_token
    and locked_at), so recover_stale() can still reclaim it, on this
    replica's next poll or another's, exactly as if the process had been
    killed outright."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")

            settings = Settings(database_url=database_url, mcp_api_key="test-key")
            container = make_async_container(ApplicationProvider(settings=settings))

            reached = asyncio.Event()
            fetcher = HangingAudioFetcher(reached)

            lifecycle = application_lifecycle(
                container,
                audio_fetcher=fetcher,
                audio_analyzer=None,
                max_retry_attempts=5,
                shutdown_grace_period=timedelta(seconds=0.3),
            )
            await lifecycle.__aenter__()
            await asyncio.wait_for(reached.wait(), timeout=5)

            loop = asyncio.get_running_loop()
            start = loop.time()
            await asyncio.wait_for(lifecycle.__aexit__(None, None, None), timeout=5)
            elapsed = loop.time() - start
            assert elapsed < 3, "shutdown must be bounded by the grace period, not hang"

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, locked_by, claim_token, locked_at "
                                "FROM audio_fetch_jobs WHERE call_id = :call_id"
                            ),
                            {"call_id": call_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "IN_PROGRESS"
                assert row["locked_by"] is not None
                assert row["claim_token"] is not None
                assert row["locked_at"] is not None

            # A crashed replica's claim is recovered the same way: another
            # worker's recover_stale() sweep reclaims it once its lease is
            # judged stale.
            async with engine.begin() as connection:
                recovered = await FetchJobRepository(connection).recover_stale(
                    locked_before=datetime.now(UTC) + timedelta(minutes=1)
                )
                assert recovered == [call_id]

            async with engine.connect() as connection:
                status = (
                    await connection.execute(
                        text("SELECT status FROM audio_fetch_jobs WHERE call_id = :call_id"),
                        {"call_id": call_id},
                    )
                ).scalar_one()
                assert status == "PENDING"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
