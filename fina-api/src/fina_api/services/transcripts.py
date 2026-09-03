from datetime import datetime
from typing import Protocol

from fina_api.repositories.types import TranscriptRecord


class TranscriptReader(Protocol):
    async def list_raw(
        self,
        *,
        advisor_phone: str,
        client_phone: str,
        date_from: datetime | None,
        date_to: datetime | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> list[TranscriptRecord]: ...

    async def list_summarized(
        self,
        *,
        advisor_phone: str,
        client_phone: str,
        date_from: datetime | None,
        date_to: datetime | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> list[TranscriptRecord]: ...
