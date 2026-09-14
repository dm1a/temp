import logging
from hmac import compare_digest
from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fina.config import Settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)


class McpKeyAuthenticator:
    """Authenticate MCP requests using the Vault-injected shared key."""

    def __init__(self, settings: Settings) -> None:
        self._expected_key = settings.mcp_api_key.get_secret_value().encode("utf-8")

    def authenticate(self, credentials: HTTPAuthorizationCredentials | None) -> None:
        if credentials is None or credentials.scheme.lower() != "bearer":
            self._raise_unauthorized("missing_bearer_credentials")

        if not compare_digest(credentials.credentials.encode("utf-8"), self._expected_key):
            self._raise_unauthorized("invalid_key")

    @staticmethod
    def _raise_unauthorized(reason: str) -> None:
        # The reason distinguishes "no credentials at all" (a misconfigured
        # or probing caller) from "a key that is simply wrong" (a stale or
        # rotated key, or a brute-force attempt) -- the presented key itself
        # is never logged, in any form.
        logger.warning("MCP authentication rejected", extra={"reason": reason})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


@inject
async def require_mcp_authentication(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    authenticator: FromDishka[McpKeyAuthenticator],
) -> None:
    authenticator.authenticate(credentials)
