import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

from fina.config import Settings
from fina.domain.clock import APPLICATION_TIMEZONE
from fina.runtime import RuntimeState
from fina.services.calls_discovery import PostgresCallsDiscoveryScheduler
from tests.conftest import application_client, make_test_application


class StubScheduler:
    def __init__(self) -> None:
        self.calls = 0

    async def schedule(self) -> bool:
        self.calls += 1
        return True


class StubTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class StubConnection:
    def begin(self) -> StubTransaction:
        return StubTransaction()


class CapturingDiscoveryRuns:
    def __init__(self) -> None:
        self.initial_window_from: datetime | None = None
        self.window_to: datetime | None = None
        self.backfill_from: datetime | None = None

    async def enqueue_next(
        self,
        *,
        initial_window_from: datetime,
        window_to: datetime,
        backfill_from: datetime | None = None,
    ) -> object:
        self.initial_window_from = initial_window_from
        self.window_to = window_to
        self.backfill_from = backfill_from
        return uuid4()


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


def test_discovery_endpoint_is_public_and_has_no_response_body() -> None:
    async def scenario() -> None:
        scheduler = StubScheduler()
        application = make_test_application(calls_discovery_scheduler=scheduler)

        async with application_client(application) as client:
            response = await client.post("/internal/tasks/discovery")

        assert response.status_code == 204
        assert response.content == b""
        assert scheduler.calls == 1

    asyncio.run(scenario())


def test_first_discovery_uses_configured_start_and_fixed_plus_three_timezone() -> None:
    async def scenario() -> None:
        now = datetime(2026, 9, 3, 12, 30, tzinfo=APPLICATION_TIMEZONE)
        repository = CapturingDiscoveryRuns()
        settings = Settings(
            database_url="postgresql+asyncpg://test:test@localhost/test",
            mcp_api_key="test-key",
            discovery_start_at="2026-09-01T00:00:00Z",
        )
        scheduler = PostgresCallsDiscoveryScheduler(
            connection=StubConnection(),  # type: ignore[arg-type]
            discovery_runs=repository,  # type: ignore[arg-type]
            settings=settings,
            runtime=RuntimeState(started=True, started_at=now - timedelta(minutes=10)),
            clock=FixedClock(now),
        )

        assert await scheduler.schedule() is True
        assert repository.initial_window_from == datetime(
            2026,
            9,
            1,
            3,
            tzinfo=APPLICATION_TIMEZONE,
        )
        assert repository.window_to == now
        assert repository.backfill_from == settings.discovery_start_at

    asyncio.run(scenario())


def test_first_discovery_falls_back_to_application_start_time() -> None:
    async def scenario() -> None:
        started_at = datetime(2026, 9, 3, 12, 0, tzinfo=APPLICATION_TIMEZONE)
        now = datetime(2026, 9, 3, 12, 30, tzinfo=APPLICATION_TIMEZONE)
        repository = CapturingDiscoveryRuns()
        scheduler = PostgresCallsDiscoveryScheduler(
            connection=StubConnection(),  # type: ignore[arg-type]
            discovery_runs=repository,  # type: ignore[arg-type]
            settings=Settings(
                database_url="postgresql+asyncpg://test:test@localhost/test",
                mcp_api_key="test-key",
            ),
            runtime=RuntimeState(started=True, started_at=started_at),
            clock=FixedClock(now),
        )

        assert await scheduler.schedule() is True
        assert repository.initial_window_from == started_at
        assert repository.window_to == now
        assert repository.backfill_from is None

    asyncio.run(scenario())
