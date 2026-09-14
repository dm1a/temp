from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fina.config import Settings

# PostgreSQL bounds each SQL statement. Worker processing deadlines separately
# bound external audio calls and the total work performed under a claim.
DATABASE_STATEMENT_TIMEOUT = timedelta(seconds=30)


def create_database_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.sql_echo,
        pool_pre_ping=True,
        # Scheduling re-reads committed state after acquiring the advisory lock.
        isolation_level="READ COMMITTED",
        connect_args={
            "server_settings": {
                "statement_timeout": str(int(DATABASE_STATEMENT_TIMEOUT.total_seconds() * 1000)),
            },
        },
    )
