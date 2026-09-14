import asyncio
from datetime import UTC, date, datetime
from uuid import UUID

from fina.domain.enums import OrderType
from fina.repositories.types import OrderRecord
from tests.conftest import MCP_AUTH_HEADERS, application_client, make_test_application


class StubOrdersReader:
    def __init__(self, records: list[OrderRecord]) -> None:
        self._records = records
        self.requested_limits: list[int] = []

    async def list(self, **kwargs: object) -> list[OrderRecord]:
        self.requested_limits.append(int(kwargs["limit"]))
        return self._records


def record(number: int) -> OrderRecord:
    return OrderRecord(
        call_id=UUID(int=number),
        source_call_id=str(number),
        advisor_phone="79990000001",
        client_phone="79990000002",
        call_started_at=datetime(2026, 9, 3, 9, number, tzinfo=UTC),
        order_type=OrderType.BUY,
        instrument_name="Сбербанк",
        volume="100 лотов",
        execution_date=date(2026, 9, 30),
        price="Рыночная",
        currency="RUB",
        additional_details=None,
    )


def test_order_page_uses_extra_row_for_has_next() -> None:
    async def scenario() -> None:
        reader = StubOrdersReader([record(1), record(2), record(3)])
        application = make_test_application(orders_reader=reader)

        async with application_client(application) as client:
            response = await client.get(
                "/api/v1/orders",
                params={"limit": 2},
                headers=MCP_AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert reader.requested_limits == [3]
        body = response.json()
        assert body["limit"] == 2
        assert body["has_next"] is True
        assert len(body["items"]) == 2
        assert body["items"][0]["order_type"] == "BUY"
        assert body["items"][0]["instrument_name"] == "Сбербанк"

    asyncio.run(scenario())


def test_order_endpoint_requires_no_query_params() -> None:
    async def scenario() -> None:
        application = make_test_application(orders_reader=StubOrdersReader([]))

        async with application_client(application) as client:
            response = await client.get("/api/v1/orders", headers=MCP_AUTH_HEADERS)

        assert response.status_code == 200
        assert response.json() == {"items": [], "limit": 50, "offset": 0, "has_next": False}

    asyncio.run(scenario())
