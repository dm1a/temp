from typing import Protocol

from fina.repositories.types import ClientProfileRecord


class ClientProfileReader(Protocol):
    async def get_customer_profile(
        self, *, client_phone: str, search: str | None
    ) -> ClientProfileRecord | None: ...
