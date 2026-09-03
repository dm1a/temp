from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Response, status

from fina_api.db.health import DatabaseProbe
from fina_api.runtime import RuntimeState

router = APIRouter(
    prefix="/internal/health",
    tags=["health"],
    route_class=DishkaRoute,
)


@router.get(
    "/live",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def liveness() -> Response:
    """Shallow process check; deliberately avoids all dependencies."""

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/startup",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Not started"}},
)
async def startup(runtime: FromDishka[RuntimeState]) -> Response:
    response_status = (
        status.HTTP_204_NO_CONTENT if runtime.started else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return Response(status_code=response_status)


@router.get(
    "/ready",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Not ready"}},
)
async def readiness(
    runtime: FromDishka[RuntimeState],
    database_probe: FromDishka[DatabaseProbe],
) -> Response:
    if not runtime.started or runtime.draining:
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        await database_probe.check()
    except Exception:  # Probe responses must not expose credentials or driver errors.
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    return Response(status_code=status.HTTP_204_NO_CONTENT)
