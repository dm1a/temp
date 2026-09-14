"""Stand-in for vault-secrets' client module.

VaultClient's real implementation (the actual Vault HTTP API calls -- AppRole
login, secret_id unwrapping, KV reads) isn't visible from outside the
corporate network, so every method here raises NotImplementedError rather
than guessing at real Vault API paths and payload shapes: getting those
wrong would look like it works locally and then silently misbehave against
a real Vault server. Delete this package (and its workspace
member/dependency entry in the root pyproject.toml) and depend on the real
internal package once this code runs inside the corporate network.
"""

import requests

from .schema import SecretId, SecretList, VaultPath, VaultToken

_NOT_IMPLEMENTED = (
    "VaultClient is a local stand-in for the internal vault-secrets package; "
    "it cannot actually talk to Vault. Replace it with the real package "
    "before running with VAULT_URL set."
)


class VaultClient:
    def __init__(self, *, session: requests.Session, base_url: str) -> None:
        self._session = session
        self._base_url = base_url

    def login(self, *, role_id: str, secret_id: SecretId) -> VaultToken:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def unwrap_secret_id(self, *, token: str) -> SecretId:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def get_secrets(self, *, engine: str, path: VaultPath, token: VaultToken) -> SecretList:
        raise NotImplementedError(_NOT_IMPLEMENTED)
