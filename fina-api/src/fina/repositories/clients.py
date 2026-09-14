from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.repositories.types import ClientProfileRecord


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

    async def list_recent_client_profiles(
        self, *, client_id: UUID, limit: int
    ) -> list[dict[str, Any]]:
        """Return up to `limit` most recent per-call client_profile.data payloads.

        Ordered by call recency (newest first), most recent call's completed
        analysis first. Only calls with a COMPLETED analysis contribute.
        """
        result = await self._connection.execute(
            text(
                """
                SELECT job.analysis_result -> 'artifacts' -> 'client_profile' -> 'data'
                FROM audio_analysis_jobs AS job
                JOIN calls AS call ON call.id = job.call_id
                WHERE call.client_id = :client_id
                  AND job.status = 'COMPLETED'
                ORDER BY call.started_at DESC
                LIMIT :limit
                """
            ),
            {"client_id": client_id, "limit": limit},
        )
        return [row[0] for row in result.all()]

    async def get_customer_profile(
        self, *, client_phone: str, search: str | None
    ) -> ClientProfileRecord | None:
        """Return the client's current profile, or None if absent or (when
        `search` is given) it doesn't match. There is no version history --
        only the latest snapshot is ever stored."""
        result = await self._connection.execute(
            text(
                """
                SELECT id, phone_normalized, customer_profile, customer_profile_updated_at
                FROM clients
                WHERE phone_normalized = :client_phone
                  AND customer_profile IS NOT NULL
                  AND (
                      CAST(:search AS TEXT) IS NULL
                      OR to_tsvector('pg_catalog.russian'::regconfig, customer_profile::text)
                         @@ websearch_to_tsquery('pg_catalog.russian'::regconfig, :search)
                  )
                """
            ),
            {"client_phone": client_phone, "search": search},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return None

        return ClientProfileRecord(
            client_id=row["id"],
            client_phone=row["phone_normalized"],
            customer_profile=row["customer_profile"],
            updated_at=row["customer_profile_updated_at"],
        )
