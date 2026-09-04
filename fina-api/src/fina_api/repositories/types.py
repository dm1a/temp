from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class DiscoveryRunClaim:
    id: UUID
    window_from: datetime
    window_to: datetime


@dataclass(frozen=True, slots=True)
class FetchJobClaim:
    call_id: UUID
    source_call_id: str
    attempts: int


@dataclass(frozen=True, slots=True)
class AnalysisJobClaim:
    call_id: UUID
    object_key: str
    attempts: int


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    call_id: UUID
    source_call_id: str
    advisor_phone: str
    client_phone: str
    started_at: datetime
    text: str
