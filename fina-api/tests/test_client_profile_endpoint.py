import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from fina.repositories.types import ClientProfileRecord
from tests.conftest import MCP_AUTH_HEADERS, application_client, make_test_application


class StubClientProfileReader:
    def __init__(self, record: ClientProfileRecord | None) -> None:
        self._record = record

    async def get_customer_profile(self, **kwargs: object) -> ClientProfileRecord | None:
        return self._record


def test_returns_current_profile_when_present() -> None:
    async def scenario() -> None:
        record = ClientProfileRecord(
            client_id=uuid4(),
            client_phone="79990000002",
            customer_profile={"theme_summary": "Облигации"},
            updated_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
        application = make_test_application(client_profile_reader=StubClientProfileReader(record))

        async with application_client(application) as client:
            response = await client.get(
                "/api/v1/client-profile",
                params={"client_phone": "79990000002"},
                headers=MCP_AUTH_HEADERS,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["limit"] == 1
        assert body["has_next"] is False
        assert len(body["items"]) == 1
        assert body["items"][0]["customer_profile"] == {"theme_summary": "Облигации"}

    asyncio.run(scenario())


def test_returns_empty_page_when_no_profile_exists() -> None:
    async def scenario() -> None:
        application = make_test_application(client_profile_reader=StubClientProfileReader(None))

        async with application_client(application) as client:
            response = await client.get(
                "/api/v1/client-profile",
                params={"client_phone": "79990000002"},
                headers=MCP_AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert response.json() == {"items": [], "limit": 1, "offset": 0, "has_next": False}

    asyncio.run(scenario())


def test_offset_beyond_the_single_snapshot_returns_empty() -> None:
    async def scenario() -> None:
        record = ClientProfileRecord(
            client_id=uuid4(),
            client_phone="79990000002",
            customer_profile={"theme_summary": "Облигации"},
            updated_at=datetime(2026, 9, 3, tzinfo=UTC),
        )
        application = make_test_application(client_profile_reader=StubClientProfileReader(record))

        async with application_client(application) as client:
            response = await client.get(
                "/api/v1/client-profile",
                params={"client_phone": "79990000002", "offset": 1},
                headers=MCP_AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert response.json()["items"] == []

    asyncio.run(scenario())
