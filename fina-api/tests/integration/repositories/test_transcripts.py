import asyncio
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from fina.config import Settings
from fina.main import create_app
from fina.repositories.transcripts import TranscriptRepository
from tests.conftest import MCP_AUTH_HEADERS, TEST_MCP_API_KEY, application_client
from tests.integration.helpers import ADVISOR, CLIENT, START, index_call, seed_call

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("method", ["list_raw", "list_summarized"])
def test_transcript_filters_search_and_pagination(database_url: str, method: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                ids = []
                for number, hour in enumerate([0, 1, 1, 2]):
                    call_id = await seed_call(
                        connection, str(number), started_at=START + timedelta(hours=hour)
                    )
                    await index_call(connection, call_id)
                    ids.append(call_id)
                for advisor, client in [
                    ("different-advisor", CLIENT),
                    (ADVISOR, "different-client"),
                ]:
                    call_id = await seed_call(
                        connection, advisor + client, advisor=advisor, client=client
                    )
                    await index_call(connection, call_id)
                await seed_call(connection, "not-indexed")

            async with engine.connect() as connection:
                read = getattr(TranscriptRepository(connection), method)
                filters = dict(
                    advisor_phone=ADVISOR,
                    client_phone=CLIENT,
                    date_from=None,
                    date_to=None,
                    search=None,
                    limit=100,
                    offset=0,
                )
                expected = [ids[3], *sorted(ids[1:3], reverse=True), ids[0]]
                records = await read(**filters)
                assert [r.call_id for r in records] == expected
                assert records[0].text == (
                    "Обсуждаем облигации" if method == "list_raw" else "Покупка акций"
                )
                assert all(r.advisor_phone == ADVISOR and r.client_phone == CLIENT for r in records)
                assert [
                    r.call_id for r in await read(**(filters | {"limit": 2, "offset": 1}))
                ] == expected[1:3]
                assert await read(**(filters | {"offset": 100})) == []
                assert [
                    r.call_id
                    for r in await read(
                        **(
                            filters
                            | {
                                "date_from": START + timedelta(hours=1),
                                "date_to": START + timedelta(hours=2),
                            }
                        )
                    )
                ] == expected[1:3]
                assert len(await read(**(filters | {"date_from": START + timedelta(hours=2)}))) == 1
                assert len(await read(**(filters | {"date_to": START + timedelta(hours=1)}))) == 1
                word = "облигация" if method == "list_raw" else "акция"
                other_word = "акция" if method == "list_raw" else "облигация"
                assert len(await read(**(filters | {"search": word}))) == 4
                assert await read(**(filters | {"search": other_word})) == []
                assert await read(**(filters | {"search": "'; DROP TABLE calls; --"})) == []
                assert await read(**(filters | {"client_phone": "unknown"})) == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_transcript_endpoints_use_real_database(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                for number in range(2):
                    call_id = await seed_call(connection, str(number))
                    await index_call(connection, call_id)
            app = create_app(
                settings=Settings(database_url=database_url, mcp_api_key=TEST_MCP_API_KEY)
            )
            async with application_client(app) as client:
                for kind, field in [("raw", "transcript"), ("summarized", "summary")]:
                    response = await client.get(
                        f"/api/v1/transcripts/{kind}",
                        params={"advisor_phone": ADVISOR, "client_phone": CLIENT, "limit": 1},
                        headers=MCP_AUTH_HEADERS,
                    )
                    assert response.status_code == 200
                    assert response.json()["has_next"] is True
                    assert len(response.json()["items"]) == 1
                    assert field in response.json()["items"][0]
        finally:
            await engine.dispose()

    asyncio.run(scenario())
