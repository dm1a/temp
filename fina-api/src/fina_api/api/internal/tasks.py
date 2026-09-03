from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Response, status

from fina_api.services.calls_discovery import CallsDiscoveryScheduler

router = APIRouter(
    prefix="/internal/tasks",
    tags=["internal-tasks"],
    route_class=DishkaRoute,
)


@router.post(
    "/discovery",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def schedule_calls_discovery(
    scheduler: FromDishka[CallsDiscoveryScheduler],
) -> Response:
    await scheduler.schedule()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
