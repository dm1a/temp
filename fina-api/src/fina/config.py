from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from vault_secrets import VaultSecretSettings

from fina.domain.clock import ApplicationDatetime

# The one prefix for every environment variable this application itself
# reads (FINA_DATABASE_URL, FINA_MCP_API_KEY, ...). Two things are
# deliberately NOT under this prefix: VAULT_* (an external system's own
# naming convention, read directly by vault_secrets -- see its docstring)
# and the test harness's FINA_TEST_DATABASE_URL (read via plain
# os.environ, unrelated to this settings machinery despite the shared
# "FINA_" text). See README's "Configuration" section.
ENV_PREFIX = "FINA_"

# Shared because pydantic does not reliably merge model_config across
# multiple bases -- Settings(DatabaseSettings, SecretSettings) would
# otherwise silently drop a base's model_config rather than inherit it.
_MODEL_CONFIG = SettingsConfigDict(
    extra="ignore",
    env_prefix=ENV_PREFIX,
    hide_input_in_errors=True,
)


class DatabaseSettings(BaseSettings):
    """Ordinary (non-secret) database configuration, usable standalone by
    migration jobs, which don't need application secrets."""

    model_config = _MODEL_CONFIG

    database_url: str
    sql_echo: bool = False


class SecretSettings(VaultSecretSettings):
    """Application secrets, kept separate from ordinary settings.

    mcp_api_key is read as a plain FINA_MCP_API_KEY env var locally, or
    supplied by Vault via VaultSecretSettings once VAULT_URL and friends
    are set (i.e. inside the corporate network) -- a no-op everywhere
    those aren't. See vault_secrets (a local stand-in package; see its
    docstring) for details.
    """

    model_config = _MODEL_CONFIG

    mcp_api_key: SecretStr = Field(min_length=1)


class Settings(DatabaseSettings, SecretSettings):
    """Full runtime settings: ordinary configuration plus secrets, all read
    from FINA_-prefixed environment variables (loaded from a local .env by
    the launch command, e.g. `uv run --env-file .env ...` -- see README's
    "Local setup" -- not by this settings machinery itself)."""

    model_config = _MODEL_CONFIG

    discovery_start_at: ApplicationDatetime | None = None
    max_retry_attempts: int = Field(default=5, ge=1)
    api_mode: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
