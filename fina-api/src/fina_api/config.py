from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fina_api.domain.clock import ApplicationDatetime


class Settings(BaseSettings):
    """Runtime settings supplied by environment variables."""

    database_url: str = "postgresql+asyncpg://fina:fina@localhost:5432/fina"
    sql_echo: bool = False
    mcp_api_key: SecretStr = Field(min_length=1)
    discovery_start_at: ApplicationDatetime | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FINA_",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
