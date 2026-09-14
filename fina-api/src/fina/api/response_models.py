from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from fina.domain.clock import ApplicationDatetime
from fina.domain.enums import OrderType


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


class OrderItem(BaseModel):
    call_id: UUID
    source_call_id: str
    call_started_at: ApplicationDatetime
    advisor_phone: str
    client_phone: str
    order_type: OrderType | None
    instrument_name: str | None
    volume: str | None
    execution_date: date | None
    price: str | None
    currency: str | None
    additional_details: str | None


class OrderPage(BaseModel):
    items: list[OrderItem]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_next: bool


class ClientProfileItem(BaseModel):
    client_phone: str
    customer_profile: dict[str, Any]
    updated_at: ApplicationDatetime


class ClientProfilePage(BaseModel):
    items: list[ClientProfileItem]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)
    has_next: bool
