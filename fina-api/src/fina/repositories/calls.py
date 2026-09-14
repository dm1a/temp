from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.domain.enums import CallDirection, ProcessingDecision
from fina.repositories.types import CallRecord


class CallRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def add_discovered_call(
        self,
        *,
        source_call_id: str,
        discovered_in_run_id: UUID,
        started_at: datetime,
        advisor_phone: str,
        counterparty_phone: str,
        call_direction: CallDirection,
        mts_filename: str,
        call_duration_sec: int,
        rec_duration_sec: int,
        processing_decision: ProcessingDecision,
        client_id: UUID | None,
        skip_reason: str | None,
    ) -> UUID:
        proposed_id = uuid4()
        result = await self._connection.execute(
            text(
                """
                INSERT INTO calls (
                    id,
                    source_call_id,
                    discovered_in_run_id,
                    started_at,
                    advisor_phone,
                    counterparty_phone,
                    call_direction,
                    mts_filename,
                    call_duration_sec,
                    rec_duration_sec,
                    client_id,
                    processing_decision,
                    skip_reason
                )
                VALUES (
                    :id,
                    :source_call_id,
                    :discovered_in_run_id,
                    :started_at,
                    :advisor_phone,
                    :counterparty_phone,
                    :call_direction,
                    :mts_filename,
                    :call_duration_sec,
                    :rec_duration_sec,
                    :client_id,
                    :processing_decision,
                    :skip_reason
                )
                ON CONFLICT (source_call_id)
                DO UPDATE SET source_call_id = EXCLUDED.source_call_id
                RETURNING id
                """
            ),
            {
                "id": proposed_id,
                "source_call_id": source_call_id,
                "discovered_in_run_id": discovered_in_run_id,
                "started_at": started_at,
                "advisor_phone": advisor_phone,
                "counterparty_phone": counterparty_phone,
                "call_direction": call_direction,
                "mts_filename": mts_filename,
                "call_duration_sec": call_duration_sec,
                "rec_duration_sec": rec_duration_sec,
                "client_id": client_id,
                "processing_decision": processing_decision,
                "skip_reason": skip_reason,
            },
        )
        return result.scalar_one()

    async def get_by_source_call_id(self, source_call_id: str) -> CallRecord | None:
        result = await self._connection.execute(
            text(
                """
                SELECT
                    id,
                    source_call_id,
                    started_at,
                    advisor_phone,
                    counterparty_phone,
                    call_direction,
                    mts_filename,
                    call_duration_sec,
                    rec_duration_sec
                FROM calls
                WHERE source_call_id = :source_call_id
                """
            ),
            {"source_call_id": source_call_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return CallRecord(
            id=row["id"],
            source_call_id=row["source_call_id"],
            started_at=row["started_at"],
            advisor_phone=row["advisor_phone"],
            counterparty_phone=row["counterparty_phone"],
            call_direction=CallDirection(row["call_direction"]),
            mts_filename=row["mts_filename"],
            call_duration_sec=row["call_duration_sec"],
            rec_duration_sec=row["rec_duration_sec"],
        )
