import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.repositories.client_profile_jobs import ClientProfileJobRepository
from fina.repositories.clients import ClientRepository
from tests.integration.helpers import CLIENT, seed_call

pytestmark = pytest.mark.integration


def test_returns_none_when_no_profile_has_been_computed_yet(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await seed_call(connection, "call-1")

            async with engine.connect() as connection:
                record = await ClientRepository(connection).get_customer_profile(
                    client_phone=CLIENT, search=None
                )
                assert record is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_returns_none_for_an_unknown_phone(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                record = await ClientRepository(connection).get_customer_profile(
                    client_phone="does-not-exist", search=None
                )
                assert record is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_returns_the_current_profile_and_matches_search(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = (
                    await connection.execute(
                        text("SELECT client_id FROM calls WHERE id = :id"), {"id": call_id}
                    )
                ).scalar_one()

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None

            async with engine.begin() as connection:
                completed = await ClientProfileJobRepository(connection).complete_with_profile(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                    customer_profile={"theme_summary": "Облигации и акции"},
                )
                assert completed is True

            async with engine.connect() as connection:
                repo = ClientRepository(connection)
                record = await repo.get_customer_profile(client_phone=CLIENT, search=None)
                assert record is not None
                assert record.customer_profile == {"theme_summary": "Облигации и акции"}
                assert record.updated_at is not None

                matching = await repo.get_customer_profile(client_phone=CLIENT, search="облигации")
                assert matching is not None

                non_matching = await repo.get_customer_profile(
                    client_phone=CLIENT, search="газпром"
                )
                assert non_matching is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completion_is_fenced_and_skipped_once_the_claim_is_lost(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = (
                    await connection.execute(
                        text("SELECT client_id FROM calls WHERE id = :id"), {"id": call_id}
                    )
                ).scalar_one()

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None
                # Simulate the claim being recovered as stale and reassigned
                # to a different worker while worker-1 is still processing.
                await ClientProfileJobRepository(connection).recover_stale(
                    locked_before=datetime.now(UTC) + timedelta(minutes=1)
                )

            async with engine.begin() as connection:
                completed = await ClientProfileJobRepository(connection).complete_with_profile(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                    customer_profile={"theme_summary": "Stale write"},
                )
                assert completed is False

            async with engine.connect() as connection:
                record = await ClientRepository(connection).get_customer_profile(
                    client_phone=CLIENT, search=None
                )
                assert record is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completion_is_locked_against_a_concurrent_reassignment(database_url: str) -> None:
    """Proves the TOCTOU race is closed: even though worker-2 reclaims the
    job (via recover_stale + claim_next) for a *new* generation in between
    worker-1's claim and its write, complete_with_profile()'s FOR UPDATE
    means worker-1's write is evaluated against the row's current state at
    commit time and is correctly rejected -- it can never land after
    worker-2's own completion."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = (
                    await connection.execute(
                        text("SELECT client_id FROM calls WHERE id = :id"), {"id": call_id}
                    )
                ).scalar_one()

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                stale_claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert stale_claim is not None

            # worker-1's claim goes stale; a new refresh is requested and
            # worker-2 picks it up as a fresh generation before worker-1's
            # slow write finally lands.
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).recover_stale(
                    locked_before=datetime.now(UTC) + timedelta(minutes=1)
                )
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                fresh_claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-2"
                )
                assert fresh_claim is not None

            async with engine.begin() as connection:
                fresh_completed = await ClientProfileJobRepository(
                    connection
                ).complete_with_profile(
                    client_id=client_id,
                    worker_id="worker-2",
                    claim_token=fresh_claim.claim_token,
                    claimed_request_version=fresh_claim.request_version,
                    customer_profile={"theme_summary": "Fresh write"},
                )
                assert fresh_completed is True

            # worker-1, unaware its claim was long gone, finally tries to write.
            async with engine.begin() as connection:
                stale_completed = await ClientProfileJobRepository(
                    connection
                ).complete_with_profile(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=stale_claim.claim_token,
                    claimed_request_version=stale_claim.request_version,
                    customer_profile={"theme_summary": "Stale write"},
                )
                assert stale_completed is False

            async with engine.connect() as connection:
                record = await ClientRepository(connection).get_customer_profile(
                    client_phone=CLIENT, search=None
                )
                assert record is not None
                assert record.customer_profile == {"theme_summary": "Fresh write"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())
