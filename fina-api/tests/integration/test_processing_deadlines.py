import asyncio
import logging
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.clock import SystemClock
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.services.analysis_worker import AnalysisWorker
from fina.services.client_profile_worker import ClientProfileWorker
from fina.services.discovery_worker import DiscoveryWorker
from fina.services.fetch_worker import FetchWorker
from fina.services.send_order_worker import SendOrderWorker
from tests.integration.helpers import ADVISOR, START, enqueue_analysis, seed_call
from tests.integration.test_client_profile_worker import seed_completed_analysis
from tests.integration.test_discovery_worker import insert_advisor
from tests.integration.test_send_order_worker import seed_send_order_job

pytestmark = pytest.mark.integration


class HangingProvider:
    def __init__(self):
        self.calls = 0
        self.cancelled = 0

    async def hang(self, *args, **kwargs):
        self.calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled += 1

    fetch_call = hang
    analyze = hang
    aggregate_client_profile = hang
    send_order = hang


async def seed_analysis(connection):
    call_id = await seed_call(connection)
    await enqueue_analysis(connection, call_id)
    return call_id


async def seed_profile(connection):
    _, client_id = await seed_completed_analysis(connection, "call-1")
    return client_id


@pytest.mark.parametrize(
    ("worker_class", "provider_argument", "table", "key", "error_code", "seed"),
    [
        (FetchWorker, "fetcher", "audio_fetch_jobs", "call_id", "FETCH_TIMEOUT", seed_call),
        (
            AnalysisWorker,
            "analyzer",
            "audio_analysis_jobs",
            "call_id",
            "ANALYZE_TIMEOUT",
            seed_analysis,
        ),
        (
            ClientProfileWorker,
            "analyzer",
            "client_profile_jobs",
            "client_id",
            "CLIENT_PROFILE_TIMEOUT",
            seed_profile,
        ),
        (
            SendOrderWorker,
            "analyzer",
            "send_order_jobs",
            "call_id",
            "SEND_ORDER_TIMEOUT",
            seed_send_order_job,
        ),
    ],
)
def test_timeout_cancels_provider_releases_claim_and_respects_retry_limit(
    database_url,
    worker_class,
    provider_argument,
    table,
    key,
    error_code,
    seed,
    caplog,
):
    caplog.set_level(logging.INFO, logger=worker_class.__module__)

    async def scenario():
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                job_id = await seed(connection)
            provider = HangingProvider()
            async with asyncio.timeout(5):
                for attempt, status in [(1, "PENDING"), (2, "FAILED")]:
                    # A different replica can pick up the retry immediately.
                    worker = worker_class(
                        engine=engine,
                        clock=SystemClock(),
                        worker_id=f"replica-{attempt}",
                        processing_timeout=timedelta(seconds=0.15),
                        max_attempts=2,
                        **{provider_argument: provider},
                    )
                    assert await worker.run_once()
                    assert provider.calls == provider.cancelled == attempt
                    event = next(
                        record
                        for record in reversed(caplog.records)
                        if getattr(record, "error_code", None) == error_code
                    )
                    assert getattr(event, key) == str(job_id)
                    assert event.worker_id == f"replica-{attempt}"
                    assert event.attempt == attempt
                    assert event.levelno == (logging.WARNING if attempt == 1 else logging.ERROR)
                    async with engine.connect() as connection:
                        row = (
                            (
                                await connection.execute(
                                    text(f"SELECT * FROM {table} WHERE {key} = :id"), {"id": job_id}
                                )
                            )
                            .mappings()
                            .one()
                        )
                        assert row["status"] == status
                        assert row["attempts"] == attempt
                        assert row["claim_token"] is None
                        assert row["locked_by"] is None
                        assert row["last_error_code"] == error_code
                        assert "deadline exceeded" in row["last_error_message"]
                assert not await worker.run_once()
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_discovery_has_one_deadline_across_advisors_and_can_be_retried(database_url):
    async def scenario():
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                run_id = await DiscoveryRunRepository(connection).create_or_get(
                    window_from=START, window_to=START + timedelta(days=1)
                )
                for phone in [ADVISOR, "79990000003", "79990000004"]:
                    await insert_advisor(connection, phone)

            class SlowFetcher:
                calls = 0
                delay = 0.1

                async def list_available_calls(self, **kwargs):
                    self.calls += 1
                    await asyncio.sleep(self.delay)
                    return []

            fetcher = SlowFetcher()
            worker = DiscoveryWorker(
                engine=engine,
                fetcher=fetcher,
                clock=SystemClock(),
                worker_id="replica-1",
                processing_timeout=timedelta(seconds=0.25),
            )
            async with asyncio.timeout(5):
                assert await worker.run_once()
            assert fetcher.calls >= 2, "the deadline must cover multiple provider calls"
            async with engine.begin() as connection:
                row = (
                    (
                        await connection.execute(
                            text("SELECT * FROM discovery_runs WHERE id = :id"), {"id": run_id}
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "FAILED"
                assert row["error_code"] == "DISCOVERY_TIMEOUT"
                assert row["claim_token"] is None
                assert (
                    await DiscoveryRunRepository(connection).enqueue_next(
                        initial_window_from=START, window_to=START + timedelta(days=1)
                    )
                    == run_id
                )
            fetcher.delay = 0
            assert await worker.run_once()
            async with engine.connect() as connection:
                assert (
                    await connection.execute(
                        text("SELECT status FROM discovery_runs WHERE id = :id"), {"id": run_id}
                    )
                ).scalar_one() == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())
