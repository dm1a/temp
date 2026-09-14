import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.audio_analysis import AnalyzeResult
from fina.repositories.analysis_jobs import AnalysisJobRepository
from fina.repositories.transcripts import TranscriptRepository
from fina.services.analysis_indexing import build_search_text
from fina.services.audio_completion import AnalysisCompletion
from tests.analysis_examples import analysis_data
from tests.integration.helpers import ADVISOR, CLIENT, enqueue_analysis, seed_call

pytestmark = pytest.mark.integration


def test_canonical_analysis_and_complete_provider_payload_round_trip_through_postgres(
    database_url: str,
) -> None:
    async def scenario() -> None:
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                call_id = await seed_call(connection, "source-123")
                await enqueue_analysis(connection, call_id)
                claim = await AnalysisJobRepository(connection).claim_next(worker_id="worker")
                assert claim is not None

            data = analysis_data(call_id)
            completion = AnalysisCompletion(claim=claim, result=AnalyzeResult.model_validate(data))
            result = completion.result
            search = build_search_text(result.artifacts)
            async with engine.begin() as connection:
                assert await AnalysisJobRepository(connection).complete_and_index(
                    call_id=completion.claim.call_id,
                    worker_id="worker",
                    claim_token=completion.claim.claim_token,
                    analysis_result=result.to_storage(),
                    analysis_schema_version=result.schema_version,
                    **search.model_dump(),
                )

            async with engine.connect() as connection:
                stored = (
                    await connection.execute(
                        text(
                            "SELECT analysis_result, analysis_schema_version "
                            "FROM audio_analysis_jobs"
                        )
                    )
                ).one()
                assert stored.analysis_result["provider_result"] == data["provider_result"]
                assert AnalyzeResult.model_validate(stored.analysis_result) == result
                assert stored.analysis_schema_version == result.schema_version
                indexed = (
                    await connection.execute(
                        text(
                            "SELECT client_profile_search "
                            # client_profile_text indexes the per-call
                            # client_profile (always present), not the
                            # aggregated customer_profile (only computed
                            # later, asynchronously, by ClientProfileWorker).
                            "@@ plainto_tsquery('russian', 'персональный') "
                            "AS profile_matches, "
                            "orders_search @@ plainto_tsquery('russian', 'Сбербанк') "
                            "AS order_matches, "
                            # orders_text now covers more than instrument_name.
                            "orders_search @@ plainto_tsquery('russian', 'реквизиты') "
                            "AS details_match "
                            "FROM call_search"
                        )
                    )
                ).one()
                assert indexed.profile_matches and indexed.order_matches
                assert indexed.details_match
                transcripts = await TranscriptRepository(connection).list_raw(
                    advisor_phone=ADVISOR,
                    client_phone=CLIENT,
                    date_from=None,
                    date_to=None,
                    search="облигации",
                    limit=10,
                    offset=0,
                )
                assert len(transcripts) == 1
                assert transcripts[0].call_id == call_id
                assert transcripts[0].text == result.artifacts.transcript.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())
