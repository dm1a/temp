from hmac import compare_digest
from typing import Annotated

from dishka.integrations.fastapi import FromDishka, inject
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fina_api.config import Settings

_bearer_scheme = HTTPBearer(auto_error=False)


class McpKeyAuthenticator:
    """Authenticate MCP requests using the Vault-injected shared key."""

    def __init__(self, settings: Settings) -> None:
        self._expected_key = settings.mcp_api_key.get_secret_value().encode("utf-8")

    def authenticate(self, credentials: HTTPAuthorizationCredentials | None) -> None:
        if credentials is None or credentials.scheme.lower() != "bearer":
            self._raise_unauthorized()

        if not compare_digest(credentials.credentials.encode("utf-8"), self._expected_key):
            self._raise_unauthorized()

    @staticmethod
    def _raise_unauthorized() -> None:
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
