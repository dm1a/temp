"""Shared FinaAPI-owned types for audio fetch and analysis adapters."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from fina.domain.clock import ApplicationDatetime
from fina.domain.enums import CallDirection

# Validate without stripping or otherwise rewriting opaque source IDs and S3 keys.
NonBlankString = Annotated[str, Field(strict=True, min_length=1, pattern=r"\S")]


class AudioContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class CallIdentity(AudioContractModel):
    """Canonical identity using the same fields and phone format as the calls table.

    Adapters must map identity from the provider's response. Copying the expected
    identity from the request would conceal a response for the wrong call.
    """

    source_call_id: NonBlankString
    started_at: ApplicationDatetime
    advisor_phone: NonBlankString
    counterparty_phone: NonBlankString
    call_direction: CallDirection


class CallIdentityMismatchError(ValueError):
    """The returned identity does not match the queued call."""
