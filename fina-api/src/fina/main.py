from fastapi import FastAPI

from fina.api.internal.tasks import router as tasks_router
from fina.api.metrics import router as metrics_router
from fina.api.probes import router as probes_router
from fina.api.v1 import router as api_v1_router
from fina.app_factory import assemble_app, build_container
from fina.config import Settings, get_settings
from fina.db.health import DatabaseProbe
from fina.domain.clock import Clock
from fina.lifespan import build_lifespan
from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.audio_fetcher import AudioFetcher
from fina.services.calls_discovery import CallsDiscoveryScheduler
from fina.services.client_profile import ClientProfileReader
from fina.services.orders import OrdersReader
from fina.services.transcripts import TranscriptReader


def create_app(
    *,
    settings: Settings | None = None,
    database_probe: DatabaseProbe | None = None,
    calls_discovery_scheduler: CallsDiscoveryScheduler | None = None,
    transcript_reader: TranscriptReader | None = None,
    orders_reader: OrdersReader | None = None,
    client_profile_reader: ClientProfileReader | None = None,
    audio_fetcher: AudioFetcher | None = None,
    audio_analyzer: AudioAnalyzer | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    """Single-app assembly: every route on one port, with its own lifespan
    managing the container's lifecycle. Used by tests (directly, with fake
    readers/scheduler/adapters/clock) and by the single-port --reload dev
    command in the README. Not what the container actually runs -- see
    fina.__main__, which needs two apps sharing one container and lifecycle
    and so can't use this factory as-is."""
    resolved_settings = settings or get_settings()
    container = build_container(
        resolved_settings,
        database_probe=database_probe,
        calls_discovery_scheduler=calls_discovery_scheduler,
        transcript_reader=transcript_reader,
        orders_reader=orders_reader,
        client_profile_reader=client_profile_reader,
        clock=clock,
    )
    return assemble_app(
        container,
        probes_router,
        tasks_router,
        api_v1_router,
        metrics_router,
        title="FinaAPI",
        lifespan=build_lifespan(
            container,
            audio_fetcher=audio_fetcher,
            audio_analyzer=audio_analyzer,
            max_retry_attempts=resolved_settings.max_retry_attempts,
        ),
    )
