import asyncio
from datetime import UTC, datetime
from uuid import UUID

from fina.repositories.types import TranscriptRecord
from tests.conftest import MCP_AUTH_HEADERS, application_client, make_test_application


class StubTranscriptReader:
    def __init__(
        self,
        *,
        raw: list[TranscriptRecord],
        summarized: list[TranscriptRecord],
    ) -> None:
        self._raw = raw
        self._summarized = summarized
        self.requested_limits: list[int] = []

    async def list_raw(self, **kwargs: object) -> list[TranscriptRecord]:
        self.requested_limits.append(int(kwargs["limit"]))
        return self._raw

    async def list_summarized(self, **kwargs: object) -> list[TranscriptRecord]:
        self.requested_limits.append(int(kwargs["limit"]))
        return self._summarized


def record(number: int, text: str) -> TranscriptRecord:
    return TranscriptRecord(
        call_id=UUID(int=number),
        source_call_id=str(number),
        advisor_phone="79990000001",
        client_phone="79990000002",
        started_at=datetime(2026, 9, 3, 9, number, tzinfo=UTC),
        text=text,
    )


def test_raw_transcript_page_uses_extra_row_for_has_next() -> None:
    async def scenario() -> None:
        reader = StubTranscriptReader(
            raw=[record(1, "one"), record(2, "two"), record(3, "three")],
            summarized=[],
        )
        application = make_test_application(transcript_reader=reader)

        async with application_client(application) as client:
            response = await client.get(
                "/api/v1/transcripts/raw",
                params={
                    "advisor_phone": "79990000001",
                    "client_phone": "79990000002",
                    "limit": 2,
                    "offset": 4,
                },
                headers=MCP_AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert reader.requested_limits == [3]
        assert response.json() == {
            "items": [
                {
                    "call_id": "00000000-0000-0000-0000-000000000001",
                    "source_call_id": "1",
                    "call_started_at": "2026-09-03T12:01:00+03:00",
                    "advisor_phone": "79990000001",
                    "client_phone": "79990000002",
                    "transcript": "one",
                },
                {
                    "call_id": "00000000-0000-0000-0000-000000000002",
                    "source_call_id": "2",
                    "call_started_at": "2026-09-03T12:02:00+03:00",
                    "advisor_phone": "79990000001",
                    "client_phone": "79990000002",
                    "transcript": "two",
                },
            ],
            "limit": 2,
            "offset": 4,
            "has_next": True,
        }

    asyncio.run(scenario())


def test_summary_page_has_summary_key_and_no_next_page() -> None:
    async def scenario() -> None:
        reader = StubTranscriptReader(raw=[], summarized=[record(1, "short summary")])
        application = make_test_application(transcript_reader=reader)

        async with application_client(application) as client:
            response = await client.get(
                "/api/v1/transcripts/summarized",
                params={
                    "advisor_phone": "79990000001",
                    "client_phone": "79990000002",
                    "limit": 2,
                },
                headers=MCP_AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert reader.requested_limits == [3]
        assert response.json()["has_next"] is False
        assert response.json()["items"][0]["summary"] == "short summary"
        assert "transcript" not in response.json()["items"][0]

    asyncio.run(scenario())
