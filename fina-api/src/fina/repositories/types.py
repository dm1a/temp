from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from fina.domain.audio_contracts import CallIdentity
from fina.domain.enums import CallDirection, OrderType


@dataclass(frozen=True, slots=True)
class DiscoveryRunClaim:
    id: UUID
    claim_token: UUID
    window_from: datetime
    window_to: datetime


@dataclass(frozen=True, slots=True)
class CallRecord:
    id: UUID
    source_call_id: str
    started_at: datetime
    advisor_phone: str
    counterparty_phone: str
    call_direction: CallDirection
    mts_filename: str
    call_duration_sec: int
    rec_duration_sec: int


@dataclass(frozen=True, slots=True)
class FetchJobClaim:
    call_id: UUID
    claim_token: UUID
    identity: CallIdentity
    attempts: int


@dataclass(frozen=True, slots=True)
class AnalysisJobClaim:
    call_id: UUID
    claim_token: UUID
    identity: CallIdentity
    manifest_object_key: str
    client_id: UUID
    attempts: int


@dataclass(frozen=True, slots=True)
class ClientProfileJobClaim:
    client_id: UUID
    claim_token: UUID
    attempts: int
    request_version: int


@dataclass(frozen=True, slots=True)
class SendOrderJobClaim:
    call_id: UUID
    claim_token: UUID
    pre_order: dict[str, Any]
    client_phone: str
    attempts: int


@dataclass(frozen=True, slots=True)
class OrderRecord:
    call_id: UUID
    source_call_id: str
    advisor_phone: str
    client_phone: str
    call_started_at: datetime
    order_type: OrderType | None
    instrument_name: str | None
    volume: str | None
    execution_date: date | None
    price: str | None
    currency: str | None
    additional_details: str | None


@dataclass(frozen=True, slots=True)
class ClientProfileRecord:
    client_id: UUID
    client_phone: str
    customer_profile: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    call_id: UUID
    source_call_id: str
    advisor_phone: str
    client_phone: str
    started_at: datetime
    text: str
