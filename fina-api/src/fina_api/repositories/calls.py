from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina_api.domain.enums import CallDirection, ProcessingDecision


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
                "client_id": client_id,
                "processing_decision": processing_decision,
                "skip_reason": skip_reason,
            },
        )
        return result.scalar_one()
