import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.repositories.advisors import AdvisorRepository
from fina.repositories.clients import ClientRepository
from tests.integration.helpers import CLIENT, seed_call

pytestmark = pytest.mark.integration


def test_duplicate_records_preserve_ids_and_client_origin(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                first = await seed_call(connection)
                assert await seed_call(connection) == first
                second = await seed_call(connection, "second")
                assert second != first
                row = (
                    await connection.execute(
                        text("SELECT id, discovered_from_call_id FROM clients")
                    )
                ).one()
                assert await ClientRepository(connection).get_or_create(CLIENT) == row.id
                assert row.discovered_from_call_id == first
                assert (
                    await connection.execute(text("SELECT count(*) FROM calls"))
                ).scalar_one() == 2
                assert (
                    await connection.execute(text("SELECT count(*) FROM audio_fetch_jobs"))
                ).scalar_one() == 2

            async def create_same_client():
                async with engine.begin() as connection:
                    return await ClientRepository(connection).get_or_create("concurrent-client")

            ids = await asyncio.wait_for(
                asyncio.gather(create_same_client(), create_same_client()), timeout=5
            )
            assert ids[0] == ids[1]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_advisor_lookup_and_service_rollback(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO advisors (phone_normalized, is_active) "
                        "VALUES ('active', true), ('inactive', false)"
                    )
                )
                repo = AdvisorRepository(connection)
                assert await repo.get_active_phones([]) == set()
                assert await repo.get_active_phones(
                    ["active", "active", "inactive", "unknown"]
                ) == {"active"}
                assert await repo.list_active_phones() == {"active"}
            with pytest.raises(RuntimeError, match="rollback"):
                async with engine.begin() as connection:
                    await seed_call(connection)
                    raise RuntimeError("rollback")
            async with engine.connect() as connection:
                for table in ("calls", "clients", "audio_fetch_jobs", "discovery_runs"):
                    assert (
                        await connection.execute(text(f"SELECT count(*) FROM {table}"))
                    ).scalar_one() == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_concurrent_discovered_calls_keep_one_record(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            # Run the whole discovery write transaction from two independent connections.
            async def discover():
                async with engine.begin() as connection:
                    return await seed_call(connection, "same-source-call")

            ids = await asyncio.wait_for(asyncio.gather(discover(), discover()), timeout=5)
            assert ids[0] == ids[1]
            async with engine.connect() as connection:
                for table in ("discovery_runs", "clients", "calls", "audio_fetch_jobs"):
                    assert (
                        await connection.execute(text(f"SELECT count(*) FROM {table}"))
                    ).scalar_one() == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
