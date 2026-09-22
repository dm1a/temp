"""Canonical analysis data and storage envelope, independent of the Analyzer SDK."""

from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, StrictBool, model_validator

from fina.domain.audio_contracts import (
    AudioContractModel,
    NonBlankString,
)
from fina.domain.clock import ApplicationDatetime
from fina.domain.enums import OrderType


class AnalyzeInput(AudioContractModel):
    task_id: UUID
    manifest_object_key: NonBlankString


class TranscriptSegment(AudioContractModel):
    start_seconds: Annotated[float, Field(ge=0)]
    end_seconds: Annotated[float, Field(ge=0)]
    text: str
    speaker: NonBlankString | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("Segment end must not precede its start")
        return self


class Transcript(AudioContractModel):
    # A successful analysis of silence may contain no recognized speech.
    text: str
    segments: tuple[TranscriptSegment, ...] = ()


class ClientProfile(AudioContractModel):
    """Per-call answers; field names remain flexible as the external contract evolves."""

    data: dict[str, JsonValue]


class CustomerProfile(AudioContractModel):
    """An accumulated profile supplied by the provider, separate from per-call answers."""

    data: dict[str, JsonValue]


class AggregateProfileResult(AudioContractModel):
    kind: Literal["success"] = "success"
    profile: CustomerProfile


class AggregateProfileError(AudioContractModel):
    kind: Literal["failure"] = "failure"
    error_code: NonBlankString
    retryable: StrictBool


AggregateProfileOutcome = Annotated[
    AggregateProfileResult | AggregateProfileError, Field(discriminator="kind")
]


class PreOrder(AudioContractModel):
    order_type: OrderType | None = None
    instrument_name: NonBlankString | None = None
    volume: NonBlankString | None = None
    execution_date: date | None = None
    price: NonBlankString | None = None
    currency: Annotated[str, Field(strict=True, pattern=r"^[A-Z]{3}$")] | None = None
    additional_details: NonBlankString | None = None


class AnalysisArtifacts(AudioContractModel):
    transcript: Transcript
    # Required on success, but explicitly empty summaries are valid.
    summary: str
    client_profile: ClientProfile
    customer_profile: CustomerProfile | None = None
    pre_order: PreOrder | None = None


class TaskIdMismatchError(ValueError):
    """The provider returned an outcome for a different analysis task."""


class AnalyzeResult(AudioContractModel):
    kind: Literal["success"] = "success"
    schema_version: Literal["1"] = "1"
    task_id: UUID
    artifacts: AnalysisArtifacts
    processed_at: ApplicationDatetime
    provider_result: dict[str, JsonValue] = Field(
        description="Complete provider success payload in JSON form, including unknown fields",
    )

    def to_storage(self) -> dict[str, JsonValue]:
        """Return the versioned JSONB envelope, with normalized and complete provider data."""
        return self.model_dump(mode="json")


class AnalyzeError(AudioContractModel):
    kind: Literal["failure"] = "failure"
    task_id: UUID
    error_code: NonBlankString
    error_message: str
    retryable: StrictBool
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None

    def validate_task(self, expected: UUID) -> None:
        if self.task_id != expected:
            raise TaskIdMismatchError("Analysis task ID does not match the queued task")


AnalyzeOutcome = Annotated[AnalyzeResult | AnalyzeError, Field(discriminator="kind")]


class SendOrderResult(AudioContractModel):
    kind: Literal["success"] = "success"


class SendOrderError(AudioContractModel):
    kind: Literal["failure"] = "failure"
    error_code: NonBlankString
    retryable: StrictBool


SendOrderOutcome = Annotated[SendOrderResult | SendOrderError, Field(discriminator="kind")]


class AnalysisSearchText(AudioContractModel):
    transcript_text: str
    summary_text: str
    client_profile_text: str
    orders_text: str
    search_schema_version: Literal["1"] = "1"
