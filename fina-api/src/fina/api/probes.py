import logging

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Response, status

from fina.db.health import DatabaseProbe
from fina.runtime import RuntimeState

logger = logging.getLogger(__name__)


def _record_readiness(runtime: RuntimeState, failure: str | None) -> None:
    if runtime.readiness_failure == failure:
        return
    runtime.readiness_failure = failure
    if failure is None:
        logger.info("readiness recovered")
    else:
        logger.warning("readiness failed", extra={"reason": failure})


router = APIRouter(
    prefix="/probes",
    tags=["probes"],
    route_class=DishkaRoute,
)


@router.get(
    "/healthz",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def healthz() -> Response:
    """Shallow liveness check; deliberately avoids all dependencies."""

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/ready",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "Not ready"}},
)
async def ready(
    runtime: FromDishka[RuntimeState],
    database_probe: FromDishka[DatabaseProbe],
) -> Response:
    """Lifecycle (started, not draining) and database connectivity/schema
    checks, preserved from the prior /internal/health/{startup,ready}
    split -- draining alone now covers what a separate startup probe did,
    since both are just "is runtime.started true"."""
    if not runtime.started or runtime.draining:
        _record_readiness(runtime, "draining" if runtime.draining else "starting")
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    try:
        await database_probe.check()
    except Exception as error:  # Neither responses nor logs expose driver messages.
        _record_readiness(runtime, type(error).__name__)
        return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

    _record_readiness(runtime, None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
