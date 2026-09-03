from pydantic import BaseModel, Field

from fina_api.domain.clock import ApplicationDatetime
from fina_api.domain.enums import DealType


class TranscriptFilters(BaseModel):
    advisor_phone: str = Field(min_length=1)
    client_phone: str = Field(min_length=1)
    date_from: ApplicationDatetime | None = None
    date_to: ApplicationDatetime | None = None
    search: str | None = Field(default=None, min_length=1)
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class DealFilters(BaseModel):
    advisor_phone: str | None = Field(default=None, min_length=1)
    client_phone: str | None = Field(default=None, min_length=1)
    date_from: ApplicationDatetime | None = None
    date_to: ApplicationDatetime | None = None
    search: str | None = Field(
        default=None,
        min_length=1,
        description="Full-text search in AudioAnalyzer pre_order.instrument_name",
    )
    deal_type: DealType | None = None
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class ClientProfileFilters(BaseModel):
    client_phone: str = Field(min_length=1)
    search: str | None = Field(
        default=None,
        min_length=1,
        description="Full-text search across all values of CustomerProfile",
    )
    limit: int = Field(default=1, ge=1)
    offset: int = Field(default=0, ge=0)
