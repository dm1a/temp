import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from fina.db.health import DatabaseProbe
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.repositories.fetch_jobs import FetchJobRepository
from tests.integration.conftest import migrate
from tests.integration.helpers import enqueue_analysis, seed_call

pytestmark = pytest.mark.integration


def test_initial_schema_constraints_and_upgrade_downgrade_round_trip(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                analyzed_call = await seed_call(connection, "analysis-call")
                await enqueue_analysis(connection, analyzed_call)
                await seed_call(connection, "fetch-call")
                repositories = {
                    "discovery_runs": DiscoveryRunRepository(connection),
                    "audio_fetch_jobs": FetchJobRepository(connection),
                    "audio_analysis_jobs": AnalysisJobRepository(connection),
                }
                tokens = {}
                for table, repository in repositories.items():
                    claim = await repository.claim_next(worker_id="worker")
                    assert claim is not None
                    tokens[table] = claim.claim_token
                    with pytest.raises(IntegrityError):
                        async with connection.begin_nested():
                            await connection.execute(text(f"UPDATE {table} SET claim_token = NULL"))
                # Reapplying head leaves existing state intact.
                await connection.run_sync(migrate)
                for table, token in tokens.items():
                    assert (
                        await connection.execute(
                            text(f"SELECT claim_token FROM {table} WHERE claim_token IS NOT NULL")
                        )
                    ).scalar_one() == token
            await DatabaseProbe(engine).check()
            async with engine.begin() as connection:
                await connection.run_sync(lambda c: migrate(c, "base", downgrade=True))
                assert (
                    await connection.execute(text("SELECT to_regclass('calls')"))
                ).scalar_one() is None
            with pytest.raises(RuntimeError, match="schema revision"):
                await DatabaseProbe(engine).check()
            async with engine.begin() as connection:
                await connection.run_sync(migrate)
            await DatabaseProbe(engine).check()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
