from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query

from fina.api.query_models import OrderFilters
from fina.api.response_models import OrderItem, OrderPage
from fina.repositories.types import OrderRecord
from fina.services.orders import OrdersReader

router = APIRouter(
    tags=["orders"],
    route_class=DishkaRoute,
)


@router.get(
    "/orders",
    response_model=OrderPage,
)
async def get_orders(
    filters: Annotated[OrderFilters, Query()],
    orders: FromDishka[OrdersReader],
) -> OrderPage:
    records = await orders.list(
        advisor_phone=filters.advisor_phone,
        client_phone=filters.client_phone,
        date_from=filters.date_from,
        date_to=filters.date_to,
        search=filters.search,
        order_type=filters.order_type,
        limit=filters.limit + 1,
        offset=filters.offset,
    )
    page_records, has_next = _page(records, filters.limit)
    return OrderPage(
        items=[
            OrderItem(
                call_id=record.call_id,
                source_call_id=record.source_call_id,
                call_started_at=record.call_started_at,
                advisor_phone=record.advisor_phone,
                client_phone=record.client_phone,
                order_type=record.order_type,
                instrument_name=record.instrument_name,
                volume=record.volume,
                execution_date=record.execution_date,
                price=record.price,
                currency=record.currency,
                additional_details=record.additional_details,
            )
            for record in page_records
        ],
        limit=filters.limit,
        offset=filters.offset,
        has_next=has_next,
    )


def _page(records: list[OrderRecord], limit: int) -> tuple[list[OrderRecord], bool]:
    return records[:limit], len(records) > limit
