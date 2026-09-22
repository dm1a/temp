from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from vault_secrets import VaultSecretSettings

from fina.domain.clock import ApplicationDatetime

ENV_PREFIX = "FINA_"

_MODEL_CONFIG = SettingsConfigDict(
    extra="ignore",
    env_prefix=ENV_PREFIX,
    hide_input_in_errors=True,
)


class DatabaseSettings(BaseSettings):
    model_config = _MODEL_CONFIG

    database_url: str
    sql_echo: bool = False


class Settings(DatabaseSettings, VaultSecretSettings):
    model_config = _MODEL_CONFIG

    discovery_start_at: ApplicationDatetime | None = None
    max_retry_attempts: int = Field(default=5, ge=1)
    api_mode: bool = True

    mcp_api_key: SecretStr = Field(min_length=1)

    s3_bucket_name: str | None = None
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_endpoint_url: str | None = None
    s3_cert_path: str | None = None

    llm_url: str | None = None
    llm_key: SecretStr | None = None
    llm_name: str | None = None
    stt_name: str | None = None
    llm_cert_path: str | None = None

    mts_secret_string: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
