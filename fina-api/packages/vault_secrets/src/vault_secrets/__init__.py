"""Stand-in for the internal corporate vault-secrets package.

The real package isn't installable outside the corporate network, so this
workspace package stands in for it: VaultSecretService, VaultAuthService,
VaultSecretSource, and VaultSecretSettings mirror the real package's public
API (as shared internally) closely enough to wire fina's Settings against
now. VaultClient (see client.py) is the part that can't be mirrored
faithfully -- its real HTTP calls aren't visible from here -- so it's a pure
stand-in that raises NotImplementedError if ever actually invoked (i.e. if
VAULT_URL is set without the real package installed).

Delete this package (and its workspace member/dependency entry in the root
pyproject.toml) and depend on the real internal package once this code runs
inside the corporate network.
"""

import os
from collections.abc import Mapping
from typing import Any

import pydantic_settings
import requests

from . import client, schema


class VaultSecretService:
    """Vault secret-reading service."""

    def __init__(self, vault: client.VaultClient, token: schema.VaultToken) -> None:
        self._vault = vault
        self._token = token

    def read(self, *, engine: str, path: schema.VaultPath) -> schema.SecretList:
        secret_list = self._vault.get_secrets(engine=engine, path=path, token=self._token)
        # Normalize keys to lowercase to match Settings' field names.
        return {key.lower(): value for key, value in secret_list.items()}


class VaultAuthService:
    """Vault authentication service."""

    def __init__(self, *, vault_url: str) -> None:
        self._vault_url = vault_url

    def __enter__(self) -> "VaultAuthService":
        self._session = requests.Session()
        self._vault = client.VaultClient(session=self._session, base_url=self._vault_url)
        return self

    def __exit__(self, *args: object) -> None:
        self._session.close()

    def unwrap_token(self, *, token: str) -> schema.SecretId:
        return self._vault.unwrap_secret_id(token=token)

    def login(self, *, role_id: str, secret_id: schema.SecretId) -> VaultSecretService:
        application_token = self._vault.login(role_id=role_id, secret_id=secret_id)
        return VaultSecretService(vault=self._vault, token=application_token)


class VaultSecretSource(pydantic_settings.EnvSettingsSource):
    """Feeds Vault-read secrets through pydantic-settings as if they were
    environment variables, so they parse/validate exactly like any other
    setting.

    Forwards **kwargs to EnvSettingsSource.__init__ instead of re-declaring
    every one of its parameters, so this stays correct across
    pydantic-settings versions without needing to track its constructor
    signature. Always uses env_prefix="" regardless of settings_cls's own
    configured prefix: secret names inside a Vault path are named by
    whoever populated that path, not by this application's own
    environment-variable-naming convention, so they should never be
    expected to carry (say) a FINA_ prefix.
    """

    def __init__(
        self,
        settings_cls: type[pydantic_settings.BaseSettings],
        vault_secrets: schema.SecretList,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self.vault_secrets = vault_secrets
        kwargs["env_prefix"] = ""
        super().__init__(settings_cls, *args, **kwargs)

    def _load_env_vars(self) -> Mapping[str, str | None]:
        return self.vault_secrets

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(vault_secrets={self.vault_secrets})"


class VaultSecretSettings(pydantic_settings.BaseSettings):
    """Mix in alongside your other BaseSettings bases to layer Vault-read
    secrets in as a settings source. A no-op (behaves exactly like plain
    BaseSettings) whenever VAULT_URL isn't set -- i.e. everywhere outside
    the corporate network today."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[pydantic_settings.BaseSettings],
        init_settings: pydantic_settings.PydanticBaseSettingsSource,
        env_settings: pydantic_settings.PydanticBaseSettingsSource,
        dotenv_settings: pydantic_settings.PydanticBaseSettingsSource,
        file_secret_settings: pydantic_settings.PydanticBaseSettingsSource,
    ) -> tuple[pydantic_settings.PydanticBaseSettingsSource, ...]:
        vault_url = os.environ.get("VAULT_URL")
        if vault_url:
            with VaultAuthService(vault_url=vault_url) as vault_auth:
                vault_secret_service = vault_auth.login(
                    role_id=os.environ["VAULT_ROLE_ID"],
                    secret_id=schema.SecretId(os.environ["VAULT_SECRET_ID"]),
                )
                vault_secrets = vault_secret_service.read(
                    engine=os.environ["VAULT_ENGINE"],
                    path=os.environ["VAULT_SECRET_PATH"],
                )
        else:
            vault_secrets = {}

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            VaultSecretSource(settings_cls, vault_secrets),
        )


__all__ = [
    "VaultAuthService",
    "VaultSecretService",
    "VaultSecretSettings",
    "VaultSecretSource",
    "client",
    "schema",
]
