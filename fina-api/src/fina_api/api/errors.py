from typing import NoReturn

from fastapi import HTTPException, status


def raise_not_implemented() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Endpoint is not implemented yet",
    )
