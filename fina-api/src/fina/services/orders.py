from datetime import datetime
from typing import Protocol

from fina.domain.enums import OrderType
from fina.repositories.types import OrderRecord


class OrdersReader(Protocol):
    async def list(
        self,
        *,
        advisor_phone: str | None,
        client_phone: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        search: str | None,
        order_type: OrderType | None,
        limit: int,
        offset: int,
    ) -> list[OrderRecord]: ...
