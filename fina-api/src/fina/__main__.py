"""python -m fina: the production entry point.

Runs two ASGI servers in one process, sharing one Dishka container and one
set of background workers:

  - routes (port 8000): the public /api/v1/* business API.
  - management (port 9000): /probes/* and /internal/tasks/* -- kept off the
    public-facing port since /internal/tasks/discovery deliberately has no
    authentication (see README's "Container deployment"). Keep this port
    cluster-private.

Shared resources (the DB engine, RuntimeState, background workers) are
initialized once and torn down once, regardless of which port serves which
routes. A startup failure in either server -- or in initializing shared
resources -- stops the whole process; a single SIGTERM/SIGINT shuts both
servers, and the workers, down together.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Generator

import uvicorn
from dishka import AsyncContainer
from fastapi import FastAPI
from pydantic import ValidationError

from fina.api.internal.tasks import router as tasks_router
from fina.api.metrics import router as metrics_router
from fina.api.probes import router as probes_router
from fina.api.v1 import router as api_v1_router
from fina.app_factory import assemble_app, build_container
from fina.config import get_settings
from fina.lifespan import application_lifecycle
from fina.logging_config import configure_logging
from fina.processing_mode import resolve_audio_adapters

logger = logging.getLogger(__name__)

ROUTES_PORT = 8000
MANAGEMENT_PORT = 9000
HTTP_SHUTDOWN_TIMEOUT = 5


class _CoordinatedServer(uvicorn.Server):
    """A uvicorn.Server whose own signal handling is disabled.

    Running two uvicorn servers in one process, each independently
    installing SIGTERM/SIGINT handlers via signal.signal(), would have the
    second one silently overwrite the first's -- so only one server would
    ever actually notice a shutdown signal. main() installs a single
    top-level handler instead and drives every server's `should_exit` from
    it, so one signal reliably stops both.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


def _build_routes_app(container: AsyncContainer) -> FastAPI:
    return assemble_app(
        container,
        api_v1_router,
        title="FinaAPI",
    )


def _build_management_app(container: AsyncContainer) -> FastAPI:
    return assemble_app(
        container,
        probes_router,
        tasks_router,
        metrics_router,
        title="FinaAPI (management)",
    )


async def run() -> None:
    settings = get_settings()
    container = build_container(settings)
    # Fail before binding either port if processing mode can't actually run.
    audio_fetcher, audio_analyzer = await resolve_audio_adapters(settings, container)

    routes_server = _CoordinatedServer(
        uvicorn.Config(
            _build_routes_app(container),
            host="0.0.0.0",
            port=ROUTES_PORT,
            lifespan="off",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=HTTP_SHUTDOWN_TIMEOUT,
        )
    )
    management_server = _CoordinatedServer(
        uvicorn.Config(
            _build_management_app(container),
            host="0.0.0.0",
            port=MANAGEMENT_PORT,
            lifespan="off",
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=HTTP_SHUTDOWN_TIMEOUT,
        )
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def shut_down_on_signal() -> None:
        await stop.wait()
        logger.info("shutdown requested, draining workers and both servers")
        begin_shutdown()
        routes_server.should_exit = True
        management_server.should_exit = True

    async def serve(server: _CoordinatedServer) -> None:
        try:
            await server.serve()
        finally:
            # A server that exits without a signal must stop its sibling too.
            stop.set()

    logger.info(
        "starting fina: routes on port %d, management on port %d, api_mode=%s",
        ROUTES_PORT,
        MANAGEMENT_PORT,
        settings.api_mode,
    )
    try:
        async with (
            application_lifecycle(
                container,
                audio_fetcher=audio_fetcher,
                audio_analyzer=audio_analyzer,
                max_retry_attempts=settings.max_retry_attempts,
            ) as begin_shutdown,
            asyncio.TaskGroup() as servers,
        ):
            servers.create_task(serve(routes_server), name="routes-server")
            servers.create_task(serve(management_server), name="management-server")
            servers.create_task(shut_down_on_signal(), name="signal-watcher")
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def _log_failure(error: BaseException) -> None:
    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            _log_failure(nested)
    elif isinstance(error, ValidationError):
        # Values, custom validation messages and exception contexts can contain
        # credentials. Log only field names and machine-readable error types.
        fields = [
            f"{'.'.join(map(str, issue['loc']))}: {issue['type']}"
            for issue in error.errors(include_input=False, include_context=False, include_url=False)
        ]
        logger.critical("fina validation failed: %s", "; ".join(fields))
    else:
        logger.critical("fina failed to start or crashed", exc_info=error)


def main() -> None:
    configure_logging()
    try:
        asyncio.run(run())
    except* Exception as excgroup:
        _log_failure(excgroup)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
