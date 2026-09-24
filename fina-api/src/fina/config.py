from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL
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

    # Alternative to a literal database_url: db_host/db_port/db_user/db_name
    # (plain env) plus db_password (env, or Vault under the key "db_password")
    # are assembled into one below. Lets the password rotate in Vault
    # independently of the non-secret connection details.
    db_host: str | None = None
    db_port: int = 5432
    db_user: str | None = None
    db_name: str | None = None
    db_password: SecretStr | None = None

    @model_validator(mode="before")
    @classmethod
    def _assemble_database_url_from_parts(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("database_url"):
            return data
        host, user, name, password = (
            data.get("db_host"),
            data.get("db_user"),
            data.get("db_name"),
            data.get("db_password"),
        )
        if host is None or user is None or name is None or password is None:
            return data
        port = data.get("db_port")
        secret = password.get_secret_value() if isinstance(password, SecretStr) else str(password)
        data = dict(data)
        data["database_url"] = URL.create(
            "postgresql+asyncpg",
            username=str(user),
            password=secret,
            host=str(host),
            port=int(port) if port else 5432,
            database=str(name),
        ).render_as_string(hide_password=False)
        return data

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
