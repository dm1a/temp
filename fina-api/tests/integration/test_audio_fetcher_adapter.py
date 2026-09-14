import asyncio
from datetime import datetime

import pytest
from audio_fetcher import (
    UTC3,
    AudioFetchError,
    AudioFetchRequest,
    AudioFetchResponse,
)
from sqlalchemy.ext.asyncio import create_async_engine

from fina.adapters.audio_fetcher_adapter import AudioFetcherAdapter
from fina.domain.audio_fetch import FetchCallInput, FetchCallResult
from fina.domain.enums import CallDirection
from tests.integration.helpers import ADVISOR, CLIENT, seed_call

pytestmark = pytest.mark.integration


class RecordingClient:
    def __init__(self, response: AudioFetchResponse | AudioFetchError) -> None:
        self.response = response
        self.requests: list[AudioFetchRequest] = []

    async def fetch_and_store(
        self, request: AudioFetchRequest
    ) -> AudioFetchResponse | AudioFetchError:
        self.requests.append(request)
        return self.response


class _UnusedClient:
    async def fetch_and_store(
        self, request: AudioFetchRequest
    ) -> AudioFetchResponse | AudioFetchError:
        raise AssertionError("fetch_and_store should not be called when no calls row exists")


def test_fetch_call_builds_request_from_seeded_call_and_maps_the_result(
    database_url: str,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(
                    connection,
                    "12345678",
                    call_direction=CallDirection.OUTBOUND,
                    mts_filename="rec.mp3",
                    call_duration_sec=90,
                    rec_duration_sec=88,
                )

            client = RecordingClient(
                AudioFetchResponse(
                    task_id=call_id,
                    call_id=12345678,
                    advisor_phone=f"+{ADVISOR}",
                    client_phone=f"+{CLIENT}",
                    advisor_is_outbound=True,
                    call_start_dt=datetime(2026, 9, 1, tzinfo=UTC3),
                    call_duration_sec=90,
                    rec_duration_sec=88,
                    mts_filename="rec.mp3",
                    object_key="calls/12345678.mp3",
                    status_code=200,
                )
            )
            adapter = AudioFetcherAdapter(engine=engine, client=client)

            outcome = await adapter.fetch_call(FetchCallInput(source_call_id="12345678"))

            assert len(client.requests) == 1
            sent = client.requests[0]
            assert sent.task_id == call_id
            assert sent.call_id == 12345678
            assert sent.advisor_phone == f"+{ADVISOR}"
            assert sent.client_phone == f"+{CLIENT}"
            assert sent.advisor_is_outbound is True
            assert sent.call_duration_sec == 90
            assert sent.rec_duration_sec == 88
            assert sent.mts_filename == "rec.mp3"

            assert isinstance(outcome, FetchCallResult)
            assert outcome.identity.source_call_id == "12345678"
            assert outcome.manifest_object_key == "calls/12345678.mp3"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_fetch_call_raises_when_no_calls_row_exists(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            adapter = AudioFetcherAdapter(engine=engine, client=_UnusedClient())
            with pytest.raises(LookupError):
                await adapter.fetch_call(FetchCallInput(source_call_id="does-not-exist"))
        finally:
            await engine.dispose()

    asyncio.run(scenario())
