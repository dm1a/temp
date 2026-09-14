from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from fina.domain.enums import CallDirection, ProcessingDecision
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.calls import CallRepository
from fina.repositories.clients import ClientRepository
from fina.repositories.discovery_runs import DiscoveryRunRepository
from fina.repositories.fetch_jobs import FetchJobRepository

START = datetime(2026, 9, 1, tzinfo=UTC)
ADVISOR = "79990000001"
CLIENT = "79990000002"


async def seed_call(
    connection: AsyncConnection,
    source_id: str = "call-1",
    *,
    started_at: datetime = START,
    advisor: str = ADVISOR,
    client: str = CLIENT,
    call_direction: CallDirection = CallDirection.INBOUND,
    mts_filename: str = "recording.mp3",
    call_duration_sec: int = 120,
    rec_duration_sec: int = 118,
) -> UUID:
    runs = DiscoveryRunRepository(connection)
    run_id = await runs.create_or_get(window_from=START, window_to=START + timedelta(days=1))
    clients = ClientRepository(connection)
    client_id = await clients.get_or_create(client)
    call_id = await CallRepository(connection).add_discovered_call(
        source_call_id=source_id,
        discovered_in_run_id=run_id,
        started_at=started_at,
        advisor_phone=advisor,
        counterparty_phone=client,
        call_direction=call_direction,
        mts_filename=mts_filename,
        call_duration_sec=call_duration_sec,
        rec_duration_sec=rec_duration_sec,
        processing_decision=ProcessingDecision.PROCESS,
        client_id=client_id,
        skip_reason=None,
    )
    await clients.set_origin_if_missing(client_id=client_id, call_id=call_id)
    await FetchJobRepository(connection).create(call_id=call_id)
    return call_id


async def enqueue_analysis(connection: AsyncConnection, call_id: UUID) -> None:
    fetch = FetchJobRepository(connection)
    claim = await fetch.claim_next(worker_id="seed")
    assert claim is not None and claim.call_id == call_id
    assert await fetch.complete_and_enqueue_analysis(
        call_id=call_id,
        worker_id="seed",
        claim_token=claim.claim_token,
        object_key=f"audio/{call_id}",
    )


async def index_call(
    connection: AsyncConnection,
    call_id: UUID,
    *,
    transcript: str = "Обсуждаем облигации",
    summary: str = "Покупка акций",
) -> None:
    await enqueue_analysis(connection, call_id)
    analysis = AnalysisJobRepository(connection)
    claim = await analysis.claim_next(worker_id="seed")
    assert claim is not None and claim.call_id == call_id
    assert await analysis.complete_and_index(
        call_id=call_id,
        worker_id="seed",
        claim_token=claim.claim_token,
        analysis_result={"transcript": transcript, "summary": summary},
        analysis_schema_version="1",
        transcript_text=transcript,
        summary_text=summary,
        client_profile_text="",
        orders_text="",
        search_schema_version="1",
    )
