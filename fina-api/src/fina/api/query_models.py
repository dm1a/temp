from typing import Self

from pydantic import BaseModel, Field, model_validator

from fina.domain.clock import ApplicationDatetime
from fina.domain.enums import OrderType

MAX_PAGE_SIZE = 100
MAX_OFFSET = 100_000


class DateRangeFilters(BaseModel):
    date_from: ApplicationDatetime | None = None
    date_to: ApplicationDatetime | None = None

    @model_validator(mode="after")
    def validate_date_range(self) -> Self:
        if (
            self.date_from is not None
            and self.date_to is not None
            and self.date_from >= self.date_to
        ):
            raise ValueError("date_from must be earlier than date_to")
        return self


class _TranscriptFilters(DateRangeFilters):
    advisor_phone: str = Field(min_length=1)
    client_phone: str = Field(min_length=1)
    search: str | None = Field(default=None, min_length=1)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)


class RawTranscriptFilters(_TranscriptFilters):
    limit: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)


class SummarizedTranscriptFilters(_TranscriptFilters):
    limit: int = Field(default=50, ge=1, le=MAX_PAGE_SIZE)


class OrderFilters(DateRangeFilters):
    advisor_phone: str | None = Field(default=None, min_length=1)
    client_phone: str | None = Field(default=None, min_length=1)
    search: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Full-text search across order_type, instrument_name, volume, "
            "price, currency, and additional_details"
        ),
    )
    order_type: OrderType | None = None
    limit: int = Field(default=50, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)


class ClientProfileFilters(BaseModel):
    client_phone: str = Field(min_length=1)
    search: str | None = Field(
        default=None,
        min_length=1,
        description="Full-text search across all values of CustomerProfile",
    )
    limit: int = Field(default=1, ge=1, le=MAX_PAGE_SIZE)
    offset: int = Field(default=0, ge=0, le=MAX_OFFSET)
