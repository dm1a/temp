from uuid import UUID

from pydantic import BaseModel, Field

from fina_api.domain.clock import ApplicationDatetime


class RawTranscriptItem(BaseModel):
    call_id: UUID
    source_call_id: str
    call_started_at: ApplicationDatetime
    advisor_phone: str
    client_phone: str
    transcript: str


class RawTranscriptPage(BaseModel):
    items: list[RawTranscriptItem]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_next: bool


class SummarizedTranscriptItem(BaseModel):
    call_id: UUID
    source_call_id: str
    call_started_at: ApplicationDatetime
    advisor_phone: str
    client_phone: str
    summary: str


class SummarizedTranscriptPage(BaseModel):
    items: list[SummarizedTranscriptItem]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_next: bool
