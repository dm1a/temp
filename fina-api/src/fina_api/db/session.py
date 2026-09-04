from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fina_api.config import Settings


def create_database_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.sql_echo,
        pool_pre_ping=True,
    )
