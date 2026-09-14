"""Stand-in for vault-secrets' schema module.

Only the shapes referenced by VaultAuthService/VaultSecretService are
mirrored here, inferred from how they're used -- the real package's
internal data shapes aren't visible from outside the corporate network.
Delete this package (and its workspace member/dependency entry in the root
pyproject.toml) and depend on the real internal package once this code runs
inside the corporate network.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import SecretStr

# The AppRole secret_id used to authenticate.
SecretId = SecretStr

# A path within a Vault secrets engine.
VaultPath = str

# Secret values read back from a Vault path, keyed by (lowercased) name.
SecretList = Mapping[str, str]


@dataclass(frozen=True, slots=True)
class VaultToken:
    """An application token returned by VaultClient.login()."""

    value: SecretStr
