from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from fina_api.config import Settings
from fina_api.db.health import DatabaseProbe
from fina_api.main import create_app
from fina_api.repositories.types import TranscriptRecord
from fina_api.services.calls_discovery import CallsDiscoveryScheduler
from fina_api.services.transcripts import TranscriptReader

TEST_MCP_API_KEY = "test-mcp-api-key"
MCP_AUTH_HEADERS = {"Authorization": f"Bearer {TEST_MCP_API_KEY}"}


class EmptyTranscriptReader:
    async def list_raw(self, **_: object) -> list[TranscriptRecord]:
        return []

    async def list_summarized(self, **_: object) -> list[TranscriptRecord]:
        return []


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
) -> FastAPI:
    return create_app(
        settings=Settings(mcp_api_key=TEST_MCP_API_KEY),
        database_probe=StubDatabaseProbe(available=database_available),
        calls_discovery_scheduler=calls_discovery_scheduler,
        transcript_reader=transcript_reader or EmptyTranscriptReader(),
    )


@asynccontextmanager
async def application_client(application: FastAPI) -> AsyncIterator[AsyncClient]:
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
