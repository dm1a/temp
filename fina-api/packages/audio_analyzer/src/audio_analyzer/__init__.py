"""Mirrors the draft FINA Analyzer SDK (final version, as shared 2026-09-07).

The real package is not published or installable yet, so this workspace
package stands in for it: enough of the draft to type-check and unit-test
audio_analyzer_adapter.py against. Delete this package (and the matching
workspace member/dependency entries in the root pyproject.toml) and depend on
the real package once it exists. analyze(), client_profile(), and
send_order() are all mirrored.
"""

import enum
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
)

NotEmptyString = Annotated[str, StringConstraints(min_length=1)]

UTC3 = timezone(timedelta(hours=3))


def enforce_utc3(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() != UTC3.utcoffset(dt):
        raise ValueError("Datetime must be in UTC+3 timezone")
    return dt


Utc3Datetime = Annotated[datetime, AfterValidator(enforce_utc3)]


def enforce_phone_plus(value: str) -> str:
    if not value.startswith("+"):
        raise ValueError("Phone number must start with '+'")
    return value


PhoneNumber = Annotated[str, BeforeValidator(enforce_phone_plus)]


class SpeakerRole(enum.StrEnum):
    ADVISOR = "ADVISOR"
    CLIENT = "CLIENT"
    UNKNOWN = "UNKNOWN"


class OrderType(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DialogUsefulness(enum.StrEnum):
    USEFUL = "USEFUL"
    NOT_USEFUL = "NOT_USEFUL"


class EmotionalTone(enum.StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ContractAction(enum.StrEnum):
    NOT_DISCUSSED = "NOT_DISCUSSED"
    TOP_UP = "TOP_UP"
    WITHDRAWAL = "WITHDRAWAL"
    TERMINATION = "TERMINATION"
    OTHER = "OTHER"


class RiskAttitude(enum.StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"
    UNKNOWN = "UNKNOWN"


class CallIdentity(BaseModel):
    mts_id_call: PositiveInt
    call_start: Utc3Datetime
    advisor_phone_number: PhoneNumber
    client_phone_number: PhoneNumber
    advisor_is_outbound: bool
    call_duration_sec: PositiveInt
    rec_duration_sec: PositiveInt


class AnalyzeInput(BaseModel):
    object_key: str


class TranscriptSegment(BaseModel):
    segment_id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: PositiveInt
    speaker: SpeakerRole
    text: str
    confidence: float = 0.0


class Transcript(BaseModel):
    full_text: NotEmptyString
    language: Literal["ru"] = "ru"
    segments: list[TranscriptSegment] = []


class DialogDescription(BaseModel):
    dialog_theme: NotEmptyString | None
    contract_action: ContractAction = ContractAction.NOT_DISCUSSED
    risk_attitude: RiskAttitude = RiskAttitude.UNKNOWN
    mentioned_assets: list[NotEmptyString] | None = None
    emotional_tone: EmotionalTone
    follow_up_meeting: bool | None
    follow_up_datetime: Utc3Datetime | None
    follow_up_comment: NotEmptyString | None
    usefulness: DialogUsefulness | None = DialogUsefulness.NOT_USEFUL
    advisor_comment: NotEmptyString | None


class PreOrder(BaseModel):
    order_type: OrderType | None = None
    instrument_name: NotEmptyString | None
    volume: NotEmptyString | None
    execution_date: date | None = None
    price: NotEmptyString | None = None
    currency: Literal["RUB"] = "RUB"
    additional_details: NotEmptyString | None


class AnalysisArtifacts(BaseModel):
    transcript: Transcript
    summary: NotEmptyString | None
    dialog_description: DialogDescription
    pre_order: PreOrder | None = None


class AnalyzeResult(BaseModel):
    task_id: str
    call_identity: CallIdentity
    artifacts: AnalysisArtifacts
    processed_at: Utc3Datetime


class AnalyzeError(BaseModel):
    task_id: str
    error_code: Literal["internal_error", "network_error"]
    retry: bool = False


class ClientProfile(BaseModel):
    """Aggregated profile returned by client_profile(); echoes the passed task_id."""

    task_id: UUID
    theme_summary: NotEmptyString | None
    contract_summary: ContractAction = ContractAction.NOT_DISCUSSED
    risk_summary: RiskAttitude = RiskAttitude.UNKNOWN
    mentioned_assets_last: list[NotEmptyString] | None = None
    emotional_tone_average: EmotionalTone
    follow_up_meeting_last_one: bool | None
    follow_up_datetime_last_one: Utc3Datetime | None


class ErrorClientProfile(BaseModel):
    task_id: UUID
    error_code: Literal["all_none", "internal_error", "network_error"] = "internal_error"
    retry: bool = False


class SendOrderInput(PreOrder):
    order_id: UUID
    client_phone_number: NotEmptyString


class ErrorSendOrder(BaseModel):
    order_id: UUID
    error_code: Literal["internal_error", "network_error"] = "internal_error"
    retry: bool = False


class FinaAnalyzerClient(Protocol):
    """The external FINA Analyzer SDK client (draft), as the adapter consumes it."""

    async def analyze(
        self, task_id: UUID, input_data: AnalyzeInput
    ) -> AnalyzeResult | AnalyzeError: ...

    async def client_profile(
        self, task_id: UUID, input_data: list[DialogDescription]
    ) -> ClientProfile | ErrorClientProfile: ...

    async def send_order(self, input_data: SendOrderInput) -> ErrorSendOrder | None: ...
