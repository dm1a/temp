import asyncio
from unittest.mock import AsyncMock

import pytest

from fina.services.analysis_worker import AnalysisWorker
from fina.services.client_profile_worker import ClientProfileWorker
from fina.services.discovery_worker import DiscoveryWorker
from fina.services.fetch_worker import FetchWorker
from fina.services.send_order_worker import SendOrderWorker


@pytest.mark.parametrize(
    ("worker_class", "provider_argument"),
    [
        (FetchWorker, "fetcher"),
        (DiscoveryWorker, "fetcher"),
        (AnalysisWorker, "analyzer"),
        (ClientProfileWorker, "analyzer"),
        (SendOrderWorker, "analyzer"),
    ],
)
def test_shutdown_during_recovery_does_not_claim_another_job(worker_class, provider_argument):
    async def scenario():
        stop = asyncio.Event()
        recovering = asyncio.Event()
        release_recovery = asyncio.Event()

        async def recover():
            recovering.set()
            await release_recovery.wait()

        worker = worker_class(
            engine=object(), clock=object(), worker_id="test", **{provider_argument: object()}
        )
        worker._recover_stale = recover
        worker.run_once = AsyncMock(return_value=False)
        task = asyncio.create_task(worker.run_forever(stop))
        try:
            await asyncio.wait_for(recovering.wait(), timeout=1)
        finally:
            stop.set()
            release_recovery.set()
            await asyncio.wait_for(task, timeout=1)
        worker.run_once.assert_not_awaited()

    asyncio.run(scenario())
