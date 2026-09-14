"""Shared pieces for building the Dishka container and a FastAPI app from it.

Used by both fina.main (the single-app create_app(), for tests and local
--reload dev) and fina.__main__ (the real two-app production entry point,
which needs one container shared by two apps and cannot use create_app()
directly for that reason -- see its own module docstring).
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from dishka import AsyncContainer, make_async_container
from dishka.integrations.fastapi import FastapiProvider, setup_dishka
from fastapi import APIRouter, FastAPI

from fina.config import Settings
from fina.db.health import DatabaseProbe
from fina.di.providers import (
    ApplicationProvider,
    CallsDiscoveryOverrideProvider,
    CallsDiscoveryProvider,
    ClientProfileOverrideProvider,
    ClientProfileProvider,
    OrdersOverrideProvider,
    OrdersProvider,
    RepositoryProvider,
    TranscriptOverrideProvider,
    TranscriptProvider,
)
from fina.domain.clock import Clock
from fina.metrics import MetricsMiddleware
from fina.services.calls_discovery import CallsDiscoveryScheduler
from fina.services.client_profile import ClientProfileReader
from fina.services.orders import OrdersReader
from fina.services.transcripts import TranscriptReader


def build_container(
    settings: Settings,
    *,
    database_probe: DatabaseProbe | None = None,
    calls_discovery_scheduler: CallsDiscoveryScheduler | None = None,
    transcript_reader: TranscriptReader | None = None,
    orders_reader: OrdersReader | None = None,
    client_profile_reader: ClientProfileReader | None = None,
    clock: Clock | None = None,
) -> AsyncContainer:
    """The override kwargs exist for tests (fake readers/scheduler/clock/probe);
    production (fina.__main__) always calls this with just settings."""
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
    orders_provider = (
        OrdersOverrideProvider(orders_reader) if orders_reader is not None else OrdersProvider()
    )
    client_profile_provider = (
        ClientProfileOverrideProvider(client_profile_reader)
        if client_profile_reader is not None
        else ClientProfileProvider()
    )
    return make_async_container(
        ApplicationProvider(settings=settings, database_probe=database_probe, clock=clock),
        RepositoryProvider(),
        calls_discovery_provider,
        transcript_provider,
        orders_provider,
        client_profile_provider,
        FastapiProvider(),
    )


def assemble_app(
    container: AsyncContainer,
    *routers: APIRouter,
    title: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """lifespan is left unset (None) by callers that manage the container's
    lifecycle externally -- see fina.__main__, which shares one lifecycle
    across two apps and disables each app's own lifespan via uvicorn.Config
    instead."""
    app = FastAPI(title=title, version="0.1.0", lifespan=lifespan)
    for router in routers:
        app.include_router(router)
    app.add_middleware(MetricsMiddleware)
    setup_dishka(container, app)
    return app
