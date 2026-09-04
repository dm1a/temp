from collections.abc import Collection

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class AdvisorRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get_active_phones(self, candidate_phones: Collection[str]) -> set[str]:
        if not candidate_phones:
            return set()

        result = await self._connection.execute(
            text(
                """
                SELECT phone_normalized
                FROM advisors
                WHERE is_active = true
                  AND phone_normalized = ANY(CAST(:candidate_phones AS TEXT[]))
                """
            ),
            {"candidate_phones": list(candidate_phones)},
        )
        return set(result.scalars())
