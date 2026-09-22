"""Mirrors the draft MTS AudioFetcher SDK (as shared 2026-09-07).

The real package is not published or installable yet, so this workspace
package stands in for it: enough of the draft to type-check and unit-test
audio_fetcher_adapter.py against. MtsAudioFetcherClient's real
implementation (the actual MTS/S3 calls) isn't visible from here, so every
method raises NotImplementedError rather than guessing at real
request/response shapes -- see vault_secrets.client.VaultClient for the
same pattern. Delete this package (and the matching workspace
member/dependency entries in the root pyproject.toml) and depend on the
real package once it exists. Field descriptions are dropped here since they
belong to the real package, not this stand-in.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    PositiveInt,
    SecretStr,
    StringConstraints,
)

UTC3 = timezone(timedelta(hours=3))


def enforce_utc3(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() != UTC3.utcoffset(dt):
        raise ValueError("Datetime must be in UTC+3 timezone")
    return dt


Utc3Datetime = Annotated[datetime, AfterValidator(enforce_utc3)]

StringNotEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def enforce_phone_plus(value: str) -> str:
    if not value.startswith("+"):
        raise ValueError("Phone number must start with '+'")
    return value


PhoneNumber = Annotated[str, BeforeValidator(enforce_phone_plus)]


class AudioMetaFetchRequest(BaseModel):
    start_range: Utc3Datetime
    end_range: Utc3Datetime
    advisor_phone: PhoneNumber


class AudioMetaFetchItem(BaseModel):
    advisor_phone: PhoneNumber
    client_phone: PhoneNumber
    advisor_is_outbound: bool = True
    call_id: PositiveInt
    call_start: Utc3Datetime
    mts_filename: StringNotEmpty
    call_duration_sec: PositiveInt
    rec_duration_sec: PositiveInt


class AudioFetchRequest(BaseModel):
    task_id: uuid.UUID
    call_id: PositiveInt
    advisor_phone: PhoneNumber
    client_phone: PhoneNumber
    advisor_is_outbound: bool
    call_start_dt: Utc3Datetime
    call_duration_sec: PositiveInt
    rec_duration_sec: PositiveInt
    mts_filename: StringNotEmpty


class AudioFetchResponse(BaseModel):
    task_id: uuid.UUID
    call_id: PositiveInt
    advisor_phone: PhoneNumber = "+79001234567"
    client_phone: PhoneNumber = "+79009876543"
    advisor_is_outbound: bool = True
    call_start_dt: Utc3Datetime
    call_duration_sec: PositiveInt
    rec_duration_sec: PositiveInt
    mts_filename: StringNotEmpty
    object_key: StringNotEmpty
    is_mp3: bool = True
    is_wav: bool = False
    status_code: Literal[200, 404, 410, 500]


class AudioFetchError(BaseModel):
    task_id: uuid.UUID
    call_id: PositiveInt = Field(default=12345678)
    status_code: Literal[200, 404, 410, 500]
    error_message: StringNotEmpty | None
    need_retry: bool


_NOT_IMPLEMENTED = (
    "MtsAudioFetcherClient is a local stand-in for the not-yet-published MTS "
    "AudioFetcher SDK; it cannot actually fetch calls. Replace it with the "
    "real package before running with FINA_API_MODE=false."
)


class MtsAudioFetcherClient:
    """The external MTS AudioFetcher SDK client (draft), as the adapter consumes it."""

    def __init__(
        self,
        s3_secret_string: SecretStr,
        mts_secret_string: SecretStr,
    ) -> None:
        self.s3_secret_string = s3_secret_string
        self.mts_secret_string = mts_secret_string

    async def list_available_calls(
        self, request: AudioMetaFetchRequest
    ) -> list[AudioMetaFetchItem]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def fetch_and_store(
        self, request: AudioFetchRequest
    ) -> AudioFetchResponse | AudioFetchError:
        raise NotImplementedError(_NOT_IMPLEMENTED)
