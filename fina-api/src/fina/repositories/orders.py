from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from fina.domain.audio_analysis import PreOrder
from fina.domain.enums import OrderType
from fina.repositories.types import OrderRecord

LIST_ORDERS_SQL = text(
    """
    SELECT
        o.call_id,
        c.source_call_id,
        c.advisor_phone,
        c.counterparty_phone AS client_phone,
        c.started_at AS call_started_at,
        o.order_type,
        o.instrument_name,
        o.volume,
        o.execution_date,
        o.price,
        o.currency,
        o.additional_details
    FROM orders AS o
    JOIN calls AS c ON c.id = o.call_id
    JOIN call_search AS s ON s.call_id = o.call_id
    WHERE (CAST(:advisor_phone AS TEXT) IS NULL OR c.advisor_phone = :advisor_phone)
      AND (CAST(:client_phone AS TEXT) IS NULL OR c.counterparty_phone = :client_phone)
      AND (CAST(:date_from AS TIMESTAMPTZ) IS NULL OR c.started_at >= :date_from)
      AND (CAST(:date_to AS TIMESTAMPTZ) IS NULL OR c.started_at < :date_to)
      AND (CAST(:order_type AS TEXT) IS NULL OR o.order_type = :order_type)
      AND (
          CAST(:search AS TEXT) IS NULL
          OR s.orders_search @@ websearch_to_tsquery('pg_catalog.russian'::regconfig, :search)
      )
    ORDER BY c.started_at DESC, c.id DESC
    LIMIT :limit
    OFFSET :offset
    """
)


class OrderRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def upsert(self, *, call_id: UUID, pre_order: PreOrder) -> None:
        """Idempotent: re-analyzing a call replaces its previous order, if any."""
        await self._connection.execute(
            text(
                """
                INSERT INTO orders (
                    call_id, order_type, instrument_name, volume,
                    execution_date, price, currency, additional_details
                )
                VALUES (
                    :call_id, :order_type, :instrument_name, :volume,
                    :execution_date, :price, :currency, :additional_details
                )
                ON CONFLICT (call_id) DO UPDATE SET
                    order_type = EXCLUDED.order_type,
                    instrument_name = EXCLUDED.instrument_name,
                    volume = EXCLUDED.volume,
                    execution_date = EXCLUDED.execution_date,
                    price = EXCLUDED.price,
                    currency = EXCLUDED.currency,
                    additional_details = EXCLUDED.additional_details
                """
            ),
            {
                "call_id": call_id,
                "order_type": pre_order.order_type.value if pre_order.order_type else None,
                "instrument_name": pre_order.instrument_name,
                "volume": pre_order.volume,
                "execution_date": pre_order.execution_date,
                "price": pre_order.price,
                "currency": pre_order.currency,
                "additional_details": pre_order.additional_details,
            },
        )

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
    ) -> list[OrderRecord]:
        result = await self._connection.execute(
            LIST_ORDERS_SQL,
            {
                "advisor_phone": advisor_phone,
                "client_phone": client_phone,
                "date_from": date_from,
                "date_to": date_to,
                "search": search,
                "order_type": order_type.value if order_type else None,
                "limit": limit,
                "offset": offset,
            },
        )
        return [
            OrderRecord(
                call_id=row["call_id"],
                source_call_id=row["source_call_id"],
                advisor_phone=row["advisor_phone"],
                client_phone=row["client_phone"],
                call_started_at=row["call_started_at"],
                order_type=OrderType(row["order_type"]) if row["order_type"] else None,
                instrument_name=row["instrument_name"],
                volume=row["volume"],
                execution_date=row["execution_date"],
                price=row["price"],
                currency=row["currency"],
                additional_details=row["additional_details"],
            )
            for row in result.mappings()
        ]
