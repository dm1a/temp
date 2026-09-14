"""FinaAPI-owned fetch contracts, independent of the AudioFetcher SDK models."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool

from fina.domain.audio_contracts import AudioContractModel, NonBlankString
from fina.domain.audio_contracts import CallIdentity as CallIdentity
from fina.domain.audio_contracts import CallIdentityMismatchError as CallIdentityMismatchError


class FetchCallInput(AudioContractModel):
    source_call_id: NonBlankString


class FetchCallResult(AudioContractModel):
    kind: Literal["success"] = "success"
    identity: CallIdentity
    manifest_object_key: NonBlankString = Field(
        description="Exact key of the uploaded JSON manifest, preserved without modification",
    )


class FetchCallError(AudioContractModel):
    kind: Literal["failure"] = "failure"
    error_code: NonBlankString
    error_message: str
    retryable: StrictBool
    http_status: Annotated[int, Field(strict=True, ge=100, le=599)] | None = None


FetchCallOutcome = Annotated[
    FetchCallResult | FetchCallError,
    Field(discriminator="kind"),
]


class DiscoveredCall(AudioContractModel):
    identity: CallIdentity
    mts_filename: NonBlankString
    call_duration_sec: Annotated[int, Field(strict=True, gt=0)]
    rec_duration_sec: Annotated[int, Field(strict=True, gt=0)]
