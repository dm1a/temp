import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fina.domain.audio_analysis import AnalyzeResult
from fina.domain.enums import OrderType
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.orders import OrderRepository
from fina.services.analysis_indexing import build_search_text
from tests.analysis_examples import analysis_data
from tests.integration.helpers import ADVISOR, CLIENT, START, enqueue_analysis, seed_call

pytestmark = pytest.mark.integration


async def seed_order(connection: AsyncConnection, source_id: str, **call_overrides: object) -> UUID:
    """Seed a call through a real, fully-shaped analysis completion, including
    the orders row AnalysisWorker._complete() would insert."""
    call_id = await seed_call(connection, source_id, **call_overrides)
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
    assert result.artifacts.pre_order is not None
    await OrderRepository(connection).upsert(call_id=call_id, pre_order=result.artifacts.pre_order)
    return call_id


def test_order_filters_search_and_pagination(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                ids = []
                for number, hour in enumerate([0, 1, 1, 2]):
                    call_id = await seed_order(
                        connection, str(number), started_at=START + timedelta(hours=hour)
                    )
                    ids.append(call_id)
                for advisor, client in [
                    ("different-advisor", CLIENT),
                    (ADVISOR, "different-client"),
                ]:
                    await seed_order(connection, advisor + client, advisor=advisor, client=client)

            async with engine.connect() as connection:
                repo = OrderRepository(connection)
                filters = dict(
                    advisor_phone=ADVISOR,
                    client_phone=CLIENT,
                    date_from=None,
                    date_to=None,
                    search=None,
                    order_type=None,
                    limit=100,
                    offset=0,
                )
                expected = [ids[3], *sorted(ids[1:3], reverse=True), ids[0]]
                records = await repo.list(**filters)
                assert [r.call_id for r in records] == expected
                assert records[0].instrument_name == "Сбербанк"
                assert records[0].order_type == OrderType.BUY
                assert all(r.advisor_phone == ADVISOR and r.client_phone == CLIENT for r in records)
                assert [
                    r.call_id for r in await repo.list(**(filters | {"limit": 2, "offset": 1}))
                ] == expected[1:3]
                assert await repo.list(**(filters | {"offset": 100})) == []
                assert len(await repo.list(**(filters | {"order_type": OrderType.BUY}))) == 4
                assert await repo.list(**(filters | {"order_type": OrderType.SELL})) == []
                assert len(await repo.list(**(filters | {"search": "Сбербанк"}))) == 4
                assert await repo.list(**(filters | {"search": "Газпром"})) == []
                assert await repo.list(**(filters | {"client_phone": "unknown"})) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())
