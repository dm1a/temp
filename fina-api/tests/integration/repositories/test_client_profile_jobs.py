import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fina.repositories.client_profile_jobs import ClientProfileJobRepository
from tests.integration.helpers import seed_call

pytestmark = pytest.mark.integration


async def client_id_for(connection: AsyncConnection, call_id) -> object:
    return (
        await connection.execute(
            text("SELECT client_id FROM calls WHERE id = :id"), {"id": call_id}
        )
    ).scalar_one()


def test_completion_requeues_instead_of_completing_when_a_newer_request_arrived(
    database_url: str,
) -> None:
    """A call finishing analysis mid-aggregation must not be lost: enqueue()
    bumps request_version even while IN_PROGRESS, and mark_completed() must
    notice that and re-queue rather than complete on stale data."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None

            # A second call for the same client finishes analysis (its own,
            # later transaction) while worker-1 is still processing the first.
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            async with engine.begin() as connection:
                completed = await ClientProfileJobRepository(connection).mark_completed(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                )
                assert completed is False

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, claim_token, locked_by FROM client_profile_jobs "
                                "WHERE client_id = :client_id"
                            ),
                            {"client_id": client_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "PENDING"
                assert row["claim_token"] is None
                assert row["locked_by"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_completion_succeeds_when_no_newer_request_arrived(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None

            async with engine.begin() as connection:
                completed = await ClientProfileJobRepository(connection).mark_completed(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                )
                assert completed is True

            async with engine.connect() as connection:
                status = (
                    await connection.execute(
                        text("SELECT status FROM client_profile_jobs WHERE client_id = :client_id"),
                        {"client_id": client_id},
                    )
                ).scalar_one()
                assert status == "COMPLETED"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enqueue_does_not_disturb_an_active_in_progress_claim(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None

            before_request_version = claim.request_version
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, claim_token, locked_by, request_version "
                                "FROM client_profile_jobs WHERE client_id = :client_id"
                            ),
                            {"client_id": client_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                # The active claim is untouched...
                assert row["status"] == "IN_PROGRESS"
                assert row["claim_token"] == claim.claim_token
                assert row["locked_by"] == "worker-1"
                # ...but the newer request is recorded for mark_completed to see.
                assert row["request_version"] > before_request_version
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_mark_failed_requeues_instead_of_failing_when_a_newer_request_arrived(
    database_url: str,
) -> None:
    """A permanent failure must not discard a call that finished (and needs
    its own attempt) during processing: mark_failed() redirects to PENDING
    instead of marking the job FAILED. attempts itself is left untouched
    here -- it's claim_next() that will notice, on the next claim, that
    request_version has moved and give the new generation a fresh budget."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            async with engine.begin() as connection:
                failed = await ClientProfileJobRepository(connection).mark_failed(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                    error_code="PERMANENT_FAILURE",
                    error_message="boom",
                )
                assert failed is False

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, attempts, last_error_code FROM client_profile_jobs "
                                "WHERE client_id = :client_id"
                            ),
                            {"client_id": client_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "PENDING"
                assert row["attempts"] == 1
                assert row["last_error_code"] is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_mark_failed_succeeds_when_no_newer_request_arrived(database_url: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None

            async with engine.begin() as connection:
                failed = await ClientProfileJobRepository(connection).mark_failed(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                    error_code="PERMANENT_FAILURE",
                    error_message="boom",
                )
                assert failed is True

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, last_error_code FROM client_profile_jobs "
                                "WHERE client_id = :client_id"
                            ),
                            {"client_id": client_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "FAILED"
                assert row["last_error_code"] == "PERMANENT_FAILURE"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_claim_next_restarts_attempts_after_a_retried_version_change(database_url: str) -> None:
    """A newer request arriving while a retryable attempt is in flight must
    not let the new generation inherit the old one's attempts count.
    schedule_retry() itself doesn't need to know about versions -- it's
    claim_next() that notices, on the next claim, that request_version has
    moved since attempts was last counted, and restarts the budget at 1."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None and claim.attempts == 1

            # A second call for the same client finishes while worker-1 is
            # still processing the first -- a newer request_version arrives
            # mid-flight.
            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            async with engine.begin() as connection:
                scheduled = await ClientProfileJobRepository(connection).schedule_retry(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    available_at=datetime.now(UTC),
                    error_code="TRANSIENT",
                    error_message="temporary",
                )
                assert scheduled is True

            async with engine.begin() as connection:
                reclaimed = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert reclaimed is not None
                assert reclaimed.request_version > claim.request_version
                assert reclaimed.attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_claim_next_restarts_attempts_after_crash_recovery_of_a_newer_version(
    database_url: str,
) -> None:
    """Reproduces the reported crash-recovery gap: worker A claims version
    1, a new call queues version 2, worker A crashes (never getting a
    chance to call schedule_retry/mark_failed at all), and recover_stale()
    -- which knows nothing about versions -- just clears the claim fields.
    Worker B's reclaim must still start version 2 at attempt 1, not
    continue counting from worker A's already-spent attempt."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-a"
                )
                assert claim is not None and claim.attempts == 1

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            # worker-a crashes without ever releasing the claim.
            async with engine.begin() as connection:
                recovered = await ClientProfileJobRepository(connection).recover_stale(
                    locked_before=datetime.now(UTC) + timedelta(minutes=1)
                )
                assert recovered == [client_id]

            async with engine.begin() as connection:
                reclaimed = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-b"
                )
                assert reclaimed is not None
                assert reclaimed.request_version > claim.request_version
                assert reclaimed.attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_enqueue_does_not_reset_attempts_immediately(database_url: str) -> None:
    """attempts is left untouched by enqueue() itself, even when the job is
    terminal -- claim_next() is the sole place that resets the budget for a
    new generation, so a terminal job's stale attempts count remains
    visible until the job is actually reclaimed."""

    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "call-1")
                client_id = await client_id_for(connection, call_id)

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)
            async with engine.begin() as connection:
                claim = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert claim is not None and claim.attempts == 1

            async with engine.begin() as connection:
                failed = await ClientProfileJobRepository(connection).mark_failed(
                    client_id=client_id,
                    worker_id="worker-1",
                    claim_token=claim.claim_token,
                    claimed_request_version=claim.request_version,
                    error_code="PERMANENT_FAILURE",
                    error_message="boom",
                )
                assert failed is True

            async with engine.begin() as connection:
                await ClientProfileJobRepository(connection).enqueue(client_id=client_id)

            async with engine.connect() as connection:
                row = (
                    (
                        await connection.execute(
                            text(
                                "SELECT status, attempts FROM client_profile_jobs "
                                "WHERE client_id = :client_id"
                            ),
                            {"client_id": client_id},
                        )
                    )
                    .mappings()
                    .one()
                )
                assert row["status"] == "PENDING"
                assert row["attempts"] == 1

            async with engine.begin() as connection:
                reclaimed = await ClientProfileJobRepository(connection).claim_next(
                    worker_id="worker-1"
                )
                assert reclaimed is not None
                assert reclaimed.attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())
