from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute
from fastapi import APIRouter, Query

from fina_api.api.errors import raise_not_implemented
from fina_api.api.query_models import ClientProfileFilters

router = APIRouter(
    tags=["client-profile"],
    route_class=DishkaRoute,
)


@router.get(
    "/client-profile",
    responses={501: {"description": "Endpoint contract exists but is not implemented"}},
)
async def get_client_profile(
    filters: Annotated[ClientProfileFilters, Query()],
) -> None:
    raise_not_implemented()
