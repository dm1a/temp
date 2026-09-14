from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fina.config import Settings
from fina.db.health import DatabaseProbe
from fina.main import create_app
from fina.repositories.types import ClientProfileRecord, OrderRecord, TranscriptRecord
from fina.services.calls_discovery import CallsDiscoveryScheduler
from fina.services.client_profile import ClientProfileReader
from fina.services.orders import OrdersReader
from fina.services.transcripts import TranscriptReader


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="Build the runtime image and run E2E tests against two app containers and PostgreSQL",
    )


TEST_MCP_API_KEY = "test-mcp-api-key"
MCP_AUTH_HEADERS = {"Authorization": f"Bearer {TEST_MCP_API_KEY}"}
# A placeholder: these api-contract tests use a StubDatabaseProbe/fake
# readers and never actually connect, but Settings.database_url has no
# default (it's meant to come from FINA_DATABASE_URL via the launch
# command, not a hardcoded fallback), so something must be supplied.
TEST_DATABASE_URL = "postgresql+asyncpg://test:test@localhost/test"


class EmptyTranscriptReader:
    async def list_raw(self, **_: object) -> list[TranscriptRecord]:
        return []

    async def list_summarized(self, **_: object) -> list[TranscriptRecord]:
        return []


class EmptyOrdersReader:
    async def list(self, **_: object) -> list[OrderRecord]:
        return []


class EmptyClientProfileReader:
    async def get_customer_profile(self, **_: object) -> ClientProfileRecord | None:
        return None


class StubDatabaseProbe(DatabaseProbe):
    def __init__(self, *, available: bool = True) -> None:
        self._available = available

    async def check(self) -> None:
        if not self._available:
            raise RuntimeError("database unavailable")


def make_test_application(
    *,
    database_available: bool = True,
    calls_discovery_scheduler: CallsDiscoveryScheduler | None = None,
    transcript_reader: TranscriptReader | None = None,
    orders_reader: OrdersReader | None = None,
    client_profile_reader: ClientProfileReader | None = None,
) -> FastAPI:
    return create_app(
        settings=Settings(database_url=TEST_DATABASE_URL, mcp_api_key=TEST_MCP_API_KEY),
        database_probe=StubDatabaseProbe(available=database_available),
        calls_discovery_scheduler=calls_discovery_scheduler,
        transcript_reader=transcript_reader or EmptyTranscriptReader(),
        orders_reader=orders_reader or EmptyOrdersReader(),
        client_profile_reader=client_profile_reader or EmptyClientProfileReader(),
    )


@asynccontextmanager
async def application_client(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
