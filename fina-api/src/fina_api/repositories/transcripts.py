from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.sql.elements import TextClause

from fina_api.repositories.types import TranscriptRecord

RAW_TRANSCRIPTS_SQL = text(
    """
    SELECT
        c.id AS call_id,
        c.source_call_id,
        c.advisor_phone,
        c.counterparty_phone AS client_phone,
        c.started_at,
        s.transcript_text AS text
    FROM calls AS c
    JOIN call_search AS s ON s.call_id = c.id
    WHERE c.advisor_phone = :advisor_phone
      AND c.counterparty_phone = :client_phone
      AND (:date_from IS NULL OR c.started_at >= :date_from)
      AND (:date_to IS NULL OR c.started_at < :date_to)
      AND (
          :search IS NULL
          OR s.transcript_search @@ websearch_to_tsquery(
              'pg_catalog.russian'::regconfig,
              :search
          )
      )
    ORDER BY c.started_at DESC, c.id DESC
    LIMIT :limit
    OFFSET :offset
    """
)

SUMMARIZED_TRANSCRIPTS_SQL = text(
    """
    SELECT
        c.id AS call_id,
        c.source_call_id,
        c.advisor_phone,
        c.counterparty_phone AS client_phone,
        c.started_at,
        s.summary_text AS text
    FROM calls AS c
    JOIN call_search AS s ON s.call_id = c.id
    WHERE c.advisor_phone = :advisor_phone
      AND c.counterparty_phone = :client_phone
      AND (:date_from IS NULL OR c.started_at >= :date_from)
      AND (:date_to IS NULL OR c.started_at < :date_to)
      AND (
          :search IS NULL
          OR s.summary_search @@ websearch_to_tsquery(
              'pg_catalog.russian'::regconfig,
              :search
          )
      )
    ORDER BY c.started_at DESC, c.id DESC
    LIMIT :limit
    OFFSET :offset
    """
)


class TranscriptRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

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
    ) -> list[TranscriptRecord]:
        return await self._list(
            statement=RAW_TRANSCRIPTS_SQL,
            advisor_phone=advisor_phone,
            client_phone=client_phone,
            date_from=date_from,
            date_to=date_to,
            search=search,
            limit=limit,
            offset=offset,
        )

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
    ) -> list[TranscriptRecord]:
        return await self._list(
            statement=SUMMARIZED_TRANSCRIPTS_SQL,
            advisor_phone=advisor_phone,
            client_phone=client_phone,
            date_from=date_from,
            date_to=date_to,
            search=search,
            limit=limit,
            offset=offset,
        )

    async def _list(
        self,
        *,
        statement: TextClause,
        advisor_phone: str,
        client_phone: str,
        date_from: datetime | None,
        date_to: datetime | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> list[TranscriptRecord]:
        result = await self._connection.execute(
            statement,
            {
                "advisor_phone": advisor_phone,
                "client_phone": client_phone,
                "date_from": date_from,
                "date_to": date_to,
                "search": search,
                "limit": limit,
                "offset": offset,
            },
        )
        return [
            TranscriptRecord(
                call_id=row["call_id"],
                source_call_id=row["source_call_id"],
                advisor_phone=row["advisor_phone"],
                client_phone=row["client_phone"],
                started_at=row["started_at"],
                text=row["text"],
            )
            for row in result.mappings()
        ]
