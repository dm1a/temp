import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.audio_contracts import CallIdentity
from fina.domain.enums import CallDirection
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.fetch_jobs import FetchJobRepository
from fina.repositories.types import AnalysisJobClaim
from tests.integration.helpers import ADVISOR, CLIENT, START, enqueue_analysis, seed_call

pytestmark = pytest.mark.integration


async def complete(repository, claim, *, token=None, worker_id="worker", **changes):
    args = dict(call_id=claim.call_id, worker_id=worker_id, claim_token=token or claim.claim_token)
    if isinstance(repository, FetchJobRepository):
        return await repository.complete_and_enqueue_analysis(
            **(args | {"object_key": f"audio/{claim.call_id}"} | changes)
        )
    return await repository.complete_and_index(
        **(
            args
            | dict(
                analysis_result={"summary": "Облигации", "items": [1, None]},
                analysis_schema_version="1",
                transcript_text="Обсуждаем облигации",
                summary_text="Купить облигации",
                client_profile_text="Инвестор",
                orders_text="Облигации",
                search_schema_version="1",
            )
            | changes
        )
    )


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
def test_claim_returns_canonical_identity_and_preserves_manifest_key(
    database_url: str, kind: str
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        cls = FetchJobRepository if kind == "fetch" else AnalysisJobRepository
        source_id = " source/+%2F/001 "
        manifest_key = " calls//Звонок +%2F/manifest.json "
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(
                    connection, source_id, call_direction=CallDirection.OUTBOUND
                )
                if kind == "analysis":
                    await enqueue_analysis(connection, call_id)
                    await connection.execute(
                        text(
                            "UPDATE audio_analysis_jobs SET object_key = :key WHERE call_id = :id"
                        ),
                        {"key": manifest_key, "id": call_id},
                    )
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL TIME ZONE 'UTC'"))
                claim = await cls(connection).claim_next(worker_id="worker")
                assert claim is not None
                assert claim.call_id == call_id
                assert claim.identity == CallIdentity(
                    source_call_id=source_id,
                    started_at=START,
                    advisor_phone=ADVISOR,
                    counterparty_phone=CLIENT,
                    call_direction=CallDirection.OUTBOUND,
                )
                assert claim.identity.started_at.isoformat() == "2026-09-01T03:00:00+03:00"
                if kind == "analysis":
                    assert isinstance(claim, AnalysisJobClaim)
                    assert claim.manifest_object_key == manifest_key
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
@pytest.mark.parametrize("field", ["source_call_id", "advisor_phone", "counterparty_phone"])
def test_invalid_stored_identity_fails_job_without_blocking_next_claim(
    database_url: str, kind: str, field: str
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        cls = FetchJobRepository if kind == "fetch" else AnalysisJobRepository
        table = "audio_fetch_jobs" if kind == "fetch" else "audio_analysis_jobs"
        try:
            async with engine.begin() as connection:
                bad_id = await seed_call(connection, "bad-call")
                if kind == "analysis":
                    await enqueue_analysis(connection, bad_id)
                good_id = await seed_call(connection, "good-call")
                if kind == "analysis":
                    await enqueue_analysis(connection, good_id)
                # Simulate malformed stored data and make it the first queued job.
                await connection.execute(
                    text(f"UPDATE calls SET {field} = :value WHERE id = :id"),
                    {"id": bad_id, "value": " \t "},
                )
                await connection.execute(
                    text(
                        f"UPDATE {table} SET available_at = now() - interval '1 minute' "
                        "WHERE call_id = :id"
                    ),
                    {"id": bad_id},
                )

            async with engine.begin() as connection:
                assert await cls(connection).claim_next(worker_id="instance-a") is None

            async with engine.begin() as connection:
                failed = (
                    (
                        await connection.execute(
                            text(f"SELECT * FROM {table} WHERE call_id = :id"), {"id": bad_id}
                        )
                    )
                    .mappings()
                    .one()
                )
                assert failed["status"] == "FAILED"
                assert failed["attempts"] == 1
                assert failed["claim_token"] is None
                assert failed["locked_by"] is None and failed["locked_at"] is None
                assert failed["last_error_code"] == "INVALID_CALL_IDENTITY"
                assert failed["last_error_message"] == "Stored call identity is invalid"

                next_claim = await cls(connection).claim_next(worker_id="instance-b")
                assert next_claim is not None and next_claim.call_id == good_id
                assert next_claim.identity.source_call_id == "good-call"
                assert next_claim.attempts == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
def test_concurrent_workers_skip_locked_jobs(database_url: str, kind: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        cls = FetchJobRepository if kind == "fetch" else AnalysisJobRepository
        try:
            async with engine.begin() as connection:
                ids = []
                for number in range(2):
                    call_id = await seed_call(connection, str(number))
                    if kind == "analysis":
                        await enqueue_analysis(connection, call_id)
                    ids.append(call_id)
            async with engine.begin() as first, engine.begin() as second:
                a = await cls(first).claim_next(worker_id="instance-a")
                assert a is not None
                # The first connection still owns its row lock when the second claims.
                b = await asyncio.wait_for(
                    cls(second).claim_next(worker_id="instance-b"), timeout=5
                )
                assert b is not None
                assert {a.call_id, b.call_id} == set(ids)
                assert a.claim_token != b.claim_token
                assert a.attempts == b.attempts == 1
                assert await cls(second).claim_next(worker_id="instance-b") is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
def test_claim_recovery_retry_and_terminal_ownership(database_url: str, kind: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        cls = FetchJobRepository if kind == "fetch" else AnalysisJobRepository
        table = "audio_fetch_jobs" if kind == "fetch" else "audio_analysis_jobs"
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection)
                if kind == "analysis":
                    await enqueue_analysis(connection, call_id)
                repo = cls(connection)
                old = await repo.claim_next(worker_id="worker")
                assert old is not None and old.attempts == 1
                locked_at = (
                    await connection.execute(text(f"SELECT locked_at FROM {table}"))
                ).scalar_one()
                assert await repo.recover_stale(locked_before=locked_at) == []
                assert await repo.recover_stale(locked_before=locked_at + timedelta(seconds=1)) == [
                    call_id
                ]
                new = await repo.claim_next(worker_id="worker")
                assert new is not None and new.attempts == 2
                assert old.claim_token != new.claim_token
                assert not await complete(repo, old)
                assert not await complete(repo, new, worker_id="other-worker")
                error = dict(
                    call_id=call_id,
                    worker_id="worker",
                    error_code="TIMEOUT",
                    error_message="retry",
                    error_http_status=504,
                )
                future = locked_at + timedelta(hours=1)
                assert not await repo.schedule_retry(
                    **error, claim_token=old.claim_token, available_at=future
                )
                assert not await repo.mark_failed(**error, claim_token=old.claim_token)
                assert not await repo.schedule_retry(
                    **(error | {"worker_id": "other-worker"}),
                    claim_token=new.claim_token,
                    available_at=future,
                )
                assert not await repo.mark_failed(
                    **(error | {"worker_id": "other-worker"}), claim_token=new.claim_token
                )
                assert await repo.schedule_retry(
                    **error, claim_token=new.claim_token, available_at=future
                )
                assert await repo.claim_next(worker_id="worker") is None
                row = (await connection.execute(text(f"SELECT * FROM {table}"))).mappings().one()
                assert row["status"] == "PENDING" and row["claim_token"] is None
                assert row["locked_at"] is None and row["locked_by"] is None
                assert row["last_error_http_status"] == 504
                assert row["available_at"] == future
                await connection.execute(text(f"UPDATE {table} SET available_at = now()"))
                retry = await repo.claim_next(worker_id="worker")
                assert retry is not None and retry.attempts == 3
                assert await repo.mark_failed(**error, claim_token=retry.claim_token)
                assert not await complete(repo, retry)
                assert await repo.recover_stale(locked_before=future) == []
                assert await repo.claim_next(worker_id="worker") is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
def test_completion_is_atomic_and_rejects_duplicate_results(database_url: str, kind: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        cls = FetchJobRepository if kind == "fetch" else AnalysisJobRepository
        table = "audio_fetch_jobs" if kind == "fetch" else "audio_analysis_jobs"
        downstream = "audio_analysis_jobs" if kind == "fetch" else "call_search"
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection)
                if kind == "analysis":
                    await enqueue_analysis(connection, call_id)
                claim = await cls(connection).claim_next(worker_id="worker")
                assert claim is not None
            # Fail the downstream insert after the job UPDATE within the same SQL statement.
            async with engine.begin() as connection:
                await connection.execute(
                    text(f"ALTER TABLE {downstream} ADD CONSTRAINT reject_result CHECK (false)")
                )
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await complete(cls(connection), claim)
            async with engine.begin() as connection:
                row = (
                    await connection.execute(text(f"SELECT status, claim_token FROM {table}"))
                ).one()
                assert row.status == "IN_PROGRESS" and row.claim_token == claim.claim_token
                assert (
                    await connection.execute(text(f"SELECT count(*) FROM {downstream}"))
                ).scalar_one() == 0
                await connection.execute(
                    text(f"ALTER TABLE {downstream} DROP CONSTRAINT reject_result")
                )
            # An outer service rollback must also roll back both effects.
            with pytest.raises(RuntimeError, match="service failed"):
                async with engine.begin() as connection:
                    assert await complete(cls(connection), claim)
                    raise RuntimeError("service failed")
            async with engine.begin() as connection:
                assert (
                    await connection.execute(text(f"SELECT status FROM {table}"))
                ).scalar_one() == "IN_PROGRESS"
                assert (
                    await connection.execute(text(f"SELECT count(*) FROM {downstream}"))
                ).scalar_one() == 0
                repo = cls(connection)
                assert await complete(repo, claim)
                assert not await complete(repo, claim)
                assert (
                    await connection.execute(text(f"SELECT count(*) FROM {downstream}"))
                ).scalar_one() == 1
                result = (await connection.execute(text(f"SELECT * FROM {table}"))).mappings().one()
                assert result["status"] == "COMPLETED" and result["claim_token"] is None
                if kind == "analysis":
                    assert result["analysis_result"] == {"summary": "Облигации", "items": [1, None]}
                    indexed = (
                        await connection.execute(
                            text("SELECT transcript_text, summary_text FROM call_search")
                        )
                    ).one()
                    assert indexed.transcript_text == "Обсуждаем облигации"
                    assert indexed.summary_text == "Купить облигации"
                else:
                    assert (
                        await connection.execute(text("SELECT object_key FROM audio_analysis_jobs"))
                    ).scalar_one() == result["object_key"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
def test_rolled_back_claim_can_be_taken_by_another_instance(database_url: str, kind: str) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        cls = FetchJobRepository if kind == "fetch" else AnalysisJobRepository
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection)
                if kind == "analysis":
                    await enqueue_analysis(connection, call_id)
            async with engine.connect() as first:
                old = await cls(first).claim_next(worker_id="instance-a")
                assert old is not None
                await first.rollback()
            async with engine.begin() as second:
                repo = cls(second)
                new = await repo.claim_next(worker_id="instance-b")
                assert new is not None and new.call_id == old.call_id
                assert new.attempts == 1
                assert new.claim_token != old.claim_token
                assert not await complete(repo, old, worker_id="instance-a")
                assert await complete(repo, new, worker_id="instance-b")
        finally:
            await engine.dispose()

    asyncio.run(scenario())
