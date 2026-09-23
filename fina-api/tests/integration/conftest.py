import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command

ROOT = Path(__file__).parents[2]


def migrate(connection: Connection, revision: str = "head", *, downgrade: bool = False) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    if downgrade:
        command.downgrade(config, revision)
    else:
        command.upgrade(config, revision)


@pytest.fixture
def database_url() -> Iterator[str]:
    """Each test owns a new database; the supplied database is never migrated or cleared."""
    server_url = os.environ.get("FINA_TEST_DATABASE_URL")
    if server_url is None:
        pytest.skip("Set FINA_TEST_DATABASE_URL to run PostgreSQL integration tests")
    base_url = make_url(server_url)
    if base_url.drivername != "postgresql+asyncpg":
        pytest.fail("FINA_TEST_DATABASE_URL must use postgresql+asyncpg")
    database_name = f"fina_test_{uuid4().hex}"
    test_url = base_url.set(database=database_name).render_as_string(hide_password=False)
    admin = create_async_engine(base_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)

    async def create() -> None:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{database_name}"'))

    async def upgrade() -> None:
        engine = create_async_engine(test_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(migrate)
        finally:
            await engine.dispose()

    async def drop() -> None:
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        finally:
            await admin.dispose()

    asyncio.run(create())
    try:
        asyncio.run(upgrade())
        yield test_url
    finally:
        asyncio.run(drop())


@pytest.fixture
def minio_endpoint_url() -> str:
    endpoint_url = os.environ.get("FINA_S3_ENDPOINT_URL")
    if endpoint_url is None:
        pytest.skip("Set FINA_S3_ENDPOINT_URL to run minio integration tests")
    return endpoint_url
