from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fina_api.db.schema import CURRENT_SCHEMA_REVISION


class DatabaseProbe:
    """Checks both PostgreSQL connectivity and the applied Alembic revision."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            revision = result.scalar_one_or_none()

            if revision != CURRENT_SCHEMA_REVISION:
                raise RuntimeError("database schema revision is not current")
