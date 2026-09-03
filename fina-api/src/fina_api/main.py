from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dishka import AsyncContainer, make_async_container
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import FastAPI

from fina_api.api.internal.health import router as health_router
from fina_api.api.internal.tasks import router as tasks_router
from fina_api.api.v1 import router as api_v1_router
from fina_api.config import Settings, get_settings
from fina_api.db.health import DatabaseProbe
from fina_api.di.providers import (
    ApplicationProvider,
    CallsDiscoveryOverrideProvider,
    CallsDiscoveryProvider,
    RepositoryProvider,
    TranscriptOverrideProvider,
    TranscriptProvider,
)
from fina_api.domain.clock import Clock
from fina_api.runtime import RuntimeState
from fina_api.services.calls_discovery import CallsDiscoveryScheduler
from fina_api.services.transcripts import TranscriptReader


def create_app(
    *,
    settings: Settings | None = None,
    database_probe: DatabaseProbe | None = None,
    calls_discovery_scheduler: CallsDiscoveryScheduler | None = None,
    transcript_reader: TranscriptReader | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    calls_discovery_provider = (
        CallsDiscoveryOverrideProvider(calls_discovery_scheduler)
        if calls_discovery_scheduler is not None
        else CallsDiscoveryProvider()
    )
    transcript_provider = (
        TranscriptOverrideProvider(transcript_reader)
        if transcript_reader is not None
        else TranscriptProvider()
    )
    container: AsyncContainer = make_async_container(
        ApplicationProvider(
            settings=resolved_settings,
            database_probe=database_probe,
            clock=clock,
        ),
        RepositoryProvider(),
        calls_discovery_provider,
        transcript_provider,
        FastapiProvider(),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime = await container.get(RuntimeState)
        application_clock = await container.get(Clock)
        runtime.started_at = application_clock.now()
        runtime.started = True
        runtime.draining = False

        try:
            yield
        finally:
            runtime.draining = True
            runtime.started = False
            await container.close()

    application = FastAPI(
        title="FinaAPI",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.include_router(health_router)
    application.include_router(tasks_router)
    application.include_router(api_v1_router)
    setup_dishka(container, application)
    return application
