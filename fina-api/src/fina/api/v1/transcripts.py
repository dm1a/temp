from typing import Annotated

from dishka.integrations.fastapi import DishkaRoute, FromDishka
from fastapi import APIRouter, Query

from fina.api.query_models import RawTranscriptFilters, SummarizedTranscriptFilters
from fina.api.response_models import (
    RawTranscriptItem,
    RawTranscriptPage,
    SummarizedTranscriptItem,
    SummarizedTranscriptPage,
)
from fina.repositories.types import TranscriptRecord
from fina.services.transcripts import TranscriptReader

router = APIRouter(
    prefix="/transcripts",
    tags=["transcripts"],
    route_class=DishkaRoute,
)


@router.get(
    "/raw",
    response_model=RawTranscriptPage,
)
async def get_raw_transcripts(
    filters: Annotated[RawTranscriptFilters, Query()],
    transcripts: FromDishka[TranscriptReader],
) -> RawTranscriptPage:
    records = await transcripts.list_raw(
        advisor_phone=filters.advisor_phone,
        client_phone=filters.client_phone,
        date_from=filters.date_from,
        date_to=filters.date_to,
        search=filters.search,
        limit=filters.limit + 1,
        offset=filters.offset,
    )
    page_records, has_next = _page(records, filters.limit)
    return RawTranscriptPage(
        items=[
            RawTranscriptItem(
                call_id=record.call_id,
                source_call_id=record.source_call_id,
                call_started_at=record.started_at,
                advisor_phone=record.advisor_phone,
                client_phone=record.client_phone,
                transcript=record.text,
            )
            for record in page_records
        ],
        limit=filters.limit,
        offset=filters.offset,
        has_next=has_next,
    )


@router.get(
    "/summarized",
    response_model=SummarizedTranscriptPage,
)
async def get_summarized_transcripts(
    filters: Annotated[SummarizedTranscriptFilters, Query()],
    transcripts: FromDishka[TranscriptReader],
) -> SummarizedTranscriptPage:
    records = await transcripts.list_summarized(
        advisor_phone=filters.advisor_phone,
        client_phone=filters.client_phone,
        date_from=filters.date_from,
        date_to=filters.date_to,
        search=filters.search,
        limit=filters.limit + 1,
        offset=filters.offset,
    )
    page_records, has_next = _page(records, filters.limit)
    return SummarizedTranscriptPage(
        items=[
            SummarizedTranscriptItem(
                call_id=record.call_id,
                source_call_id=record.source_call_id,
                call_started_at=record.started_at,
                advisor_phone=record.advisor_phone,
                client_phone=record.client_phone,
                summary=record.text,
            )
            for record in page_records
        ],
        limit=filters.limit,
        offset=filters.offset,
        has_next=has_next,
    )


def _page(
    records: list[TranscriptRecord],
    limit: int,
) -> tuple[list[TranscriptRecord], bool]:
    return records[:limit], len(records) > limit
