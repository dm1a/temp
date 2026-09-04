from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class ClientRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get_or_create(self, phone_normalized: str) -> UUID:
        proposed_id = uuid4()
        result = await self._connection.execute(
            text(
                """
                INSERT INTO clients (id, phone_normalized)
                VALUES (:id, :phone_normalized)
                ON CONFLICT (phone_normalized)
                DO UPDATE SET phone_normalized = EXCLUDED.phone_normalized
                RETURNING id
                """
            ),
            {
                "id": proposed_id,
                "phone_normalized": phone_normalized,
            },
        )
        return result.scalar_one()

    async def set_origin_if_missing(self, *, client_id: UUID, call_id: UUID) -> None:
        await self._connection.execute(
            text(
                """
                UPDATE clients
                SET discovered_from_call_id = :call_id
                WHERE id = :client_id
                  AND discovered_from_call_id IS NULL
                """
            ),
            {"client_id": client_id, "call_id": call_id},
        )
