from datetime import datetime, timedelta, timezone
from typing import Annotated, Protocol

from pydantic import AfterValidator, AwareDatetime

APPLICATION_TIMEZONE = timezone(timedelta(hours=3), name="UTC+03:00")


def to_application_timezone(value: datetime) -> datetime:
    """Represent a timezone-aware instant in FinaAPI's fixed UTC+03:00 timezone."""

    return value.astimezone(APPLICATION_TIMEZONE)


ApplicationDatetime = Annotated[
    AwareDatetime,
    AfterValidator(to_application_timezone),
]


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(APPLICATION_TIMEZONE)
