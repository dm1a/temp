from collections.abc import AsyncIterator

from dishka import Provider, Scope, provide
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fina_api.config import Settings
from fina_api.db.health import DatabaseProbe
from fina_api.db.session import create_database_engine
from fina_api.domain.clock import Clock, SystemClock
from fina_api.repositories.advisors import AdvisorRepository
from fina_api.repositories.analysis_jobs import AnalysisJobRepository
from fina_api.repositories.calls import CallRepository
from fina_api.repositories.clients import ClientRepository
from fina_api.repositories.discovery_runs import DiscoveryRunRepository
from fina_api.repositories.fetch_jobs import FetchJobRepository
from fina_api.repositories.transcripts import TranscriptRepository
from fina_api.runtime import RuntimeState
from fina_api.security.mcp import McpKeyAuthenticator
from fina_api.services.calls_discovery import (
    CallsDiscoveryScheduler,
    PostgresCallsDiscoveryScheduler,
)
from fina_api.services.transcripts import TranscriptReader


class ApplicationProvider(Provider):
    mcp_key_authenticator = provide(McpKeyAuthenticator, scope=Scope.APP)

    def __init__(
        self,
        *,
        settings: Settings,
        database_probe: DatabaseProbe | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._database_probe = database_probe
        self._clock = clock or SystemClock()
        self._runtime = RuntimeState()

    @provide(scope=Scope.APP)
    def settings(self) -> Settings:
        return self._settings

    @provide(scope=Scope.APP)
    def runtime(self) -> RuntimeState:
        return self._runtime

    @provide(scope=Scope.APP)
    def clock(self) -> Clock:
        return self._clock

    @provide(scope=Scope.APP)
    async def engine(self, settings: Settings) -> AsyncIterator[AsyncEngine]:
        engine = create_database_engine(settings)
        try:
            yield engine
        finally:
            await engine.dispose()

    @provide(scope=Scope.APP)
    def database_probe(self, engine: AsyncEngine) -> DatabaseProbe:
        return self._database_probe or DatabaseProbe(engine)

    @provide(scope=Scope.REQUEST)
    async def connection(self, engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
        async with engine.connect() as connection:
            yield connection

class CallsDiscoveryProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def scheduler(
        self,
        connection: AsyncConnection,
        discovery_runs: DiscoveryRunRepository,
        settings: Settings,
        runtime: RuntimeState,
        clock: Clock,
    ) -> CallsDiscoveryScheduler:
        return PostgresCallsDiscoveryScheduler(
            connection=connection,
            discovery_runs=discovery_runs,
            settings=settings,
            runtime=runtime,
            clock=clock,
        )


class CallsDiscoveryOverrideProvider(Provider):
    def __init__(self, scheduler: CallsDiscoveryScheduler) -> None:
        super().__init__()
        self._scheduler = scheduler

    @provide(scope=Scope.REQUEST)
    def scheduler(self) -> CallsDiscoveryScheduler:
        return self._scheduler


class TranscriptProvider(Provider):
    @provide(scope=Scope.REQUEST)
    def reader(self, repository: TranscriptRepository) -> TranscriptReader:
        return repository


class TranscriptOverrideProvider(Provider):
    def __init__(self, reader: TranscriptReader) -> None:
        super().__init__()
        self._reader = reader

    @provide(scope=Scope.REQUEST)
    def reader(self) -> TranscriptReader:
        return self._reader


class RepositoryProvider(Provider):
    advisor_repository = provide(AdvisorRepository, scope=Scope.REQUEST)
    analysis_job_repository = provide(AnalysisJobRepository, scope=Scope.REQUEST)
    call_repository = provide(CallRepository, scope=Scope.REQUEST)
    client_repository = provide(ClientRepository, scope=Scope.REQUEST)
    discovery_run_repository = provide(DiscoveryRunRepository, scope=Scope.REQUEST)
    fetch_job_repository = provide(FetchJobRepository, scope=Scope.REQUEST)
    transcript_repository = provide(TranscriptRepository, scope=Scope.REQUEST)
