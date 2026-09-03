from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute
from fastapi import APIRouter, Query

from fina_api.api.errors import raise_not_implemented
from fina_api.api.query_models import DealFilters

router = APIRouter(
    tags=["deals"],
    route_class=DishkaRoute,
)


@router.get(
    "/deals",
    responses={501: {"description": "Endpoint contract exists but is not implemented"}},
)
async def get_deals(filters: Annotated[DealFilters, Query()]) -> None:
    raise_not_implemented()
