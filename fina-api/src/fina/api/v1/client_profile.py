from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query

from fina.api.query_models import ClientProfileFilters
from fina.api.response_models import ClientProfileItem, ClientProfilePage
from fina.services.client_profile import ClientProfileReader

router = APIRouter(
    tags=["client-profile"],
    route_class=DishkaRoute,
)


@router.get(
    "/client-profile",
    response_model=ClientProfilePage,
)
async def get_client_profile(
    filters: Annotated[ClientProfileFilters, Query()],
    profiles: FromDishka[ClientProfileReader],
) -> ClientProfilePage:
    # No version history is stored -- only the current snapshot, if any -- so
    # there is at most one item regardless of limit/offset. An offset beyond
    # that single item yields an empty page.
    record = await profiles.get_customer_profile(
        client_phone=filters.client_phone, search=filters.search
    )
    items = (
        [
            ClientProfileItem(
                client_phone=record.client_phone,
                customer_profile=record.customer_profile,
                updated_at=record.updated_at,
            )
        ]
        if record is not None and filters.offset == 0
        else []
    )
    return ClientProfilePage(
        items=items,
        limit=filters.limit,
        offset=filters.offset,
        has_next=False,
    )
