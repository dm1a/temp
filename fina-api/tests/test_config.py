import pytest
from pydantic import ValidationError

from fina.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_settings_reads_documented_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")
    monkeypatch.setenv("FINA_SQL_ECHO", "true")
    monkeypatch.setenv("FINA_DISCOVERY_START_AT", "2026-09-01T00:00:00+03:00")
    monkeypatch.setenv("FINA_MAX_RETRY_ATTEMPTS", "7")
    monkeypatch.setenv("FINA_API_MODE", "false")

    settings = get_settings()

    assert settings.database_url == "postgresql+asyncpg://u:p@db-host:5432/dbname"
    assert settings.mcp_api_key.get_secret_value() == "env-provided-key"
    assert settings.sql_echo is True
    assert settings.discovery_start_at is not None
    assert settings.discovery_start_at.isoformat() == "2026-09-01T00:00:00+03:00"
    assert settings.max_retry_attempts == 7
    assert settings.api_mode is False


def test_api_mode_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("FINA_API_MODE", raising=False)
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert settings.api_mode is True


def test_unprefixed_environment_variables_are_ignored(monkeypatch) -> None:
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://should-not-be-read@host/db")
    monkeypatch.setenv("MCP_API_KEY", "should-not-be-read")

    settings = get_settings()

    assert settings.database_url == "postgresql+asyncpg://u:p@db-host:5432/dbname"
    assert settings.mcp_api_key.get_secret_value() == "env-provided-key"


def test_settings_requires_database_url(monkeypatch) -> None:
    monkeypatch.delenv("FINA_DATABASE_URL", raising=False)
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    with pytest.raises(ValidationError) as excinfo:
        get_settings()
    assert excinfo.value.errors()[0]["loc"] == ("database_url",)
    assert "env-provided-key" not in str(excinfo.value)


def test_settings_requires_mcp_api_key(monkeypatch) -> None:
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.delenv("FINA_MCP_API_KEY", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        get_settings()
    assert excinfo.value.errors()[0]["loc"] == ("mcp_api_key",)
    assert "postgresql+asyncpg://u:p@db-host:5432/dbname" not in str(excinfo.value)


def test_settings_errors_hide_unparsed_input() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings.model_validate({"mcp_api_key": {"secret": "private-value-canary"}})
    assert "private-value-canary" not in str(excinfo.value)


def test_startup_without_vault_reads_mcp_api_key_from_env(monkeypatch) -> None:
    monkeypatch.delenv("VAULT_URL", raising=False)
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert settings.mcp_api_key.get_secret_value() == "env-provided-key"


def test_database_url_is_assembled_from_host_user_name_and_password(monkeypatch) -> None:
    monkeypatch.delenv("FINA_DATABASE_URL", raising=False)
    monkeypatch.setenv("FINA_DB_HOST", "db-host")
    monkeypatch.setenv("FINA_DB_PORT", "6543")
    monkeypatch.setenv("FINA_DB_USER", "u")
    monkeypatch.setenv("FINA_DB_NAME", "dbname")
    monkeypatch.setenv("FINA_DB_PASSWORD", "p")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert settings.database_url == "postgresql+asyncpg://u:p@db-host:6543/dbname"


def test_database_url_parts_default_to_port_5432_and_url_encode_credentials(monkeypatch) -> None:
    monkeypatch.delenv("FINA_DATABASE_URL", raising=False)
    monkeypatch.delenv("FINA_DB_PORT", raising=False)
    monkeypatch.setenv("FINA_DB_HOST", "db-host")
    monkeypatch.setenv("FINA_DB_USER", "u@corp")
    monkeypatch.setenv("FINA_DB_NAME", "dbname")
    monkeypatch.setenv("FINA_DB_PASSWORD", "p@ss/word")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert (
        settings.database_url == "postgresql+asyncpg://u%40corp:p%40ss%2Fword@db-host:5432/dbname"
    )


def test_explicit_database_url_takes_precedence_over_parts(monkeypatch) -> None:
    monkeypatch.setenv(
        "FINA_DATABASE_URL", "postgresql+asyncpg://literal:literal@literal-host/literal"
    )
    monkeypatch.setenv("FINA_DB_HOST", "should-not-be-used")
    monkeypatch.setenv("FINA_DB_USER", "should-not-be-used")
    monkeypatch.setenv("FINA_DB_NAME", "should-not-be-used")
    monkeypatch.setenv("FINA_DB_PASSWORD", "should-not-be-used")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert settings.database_url == "postgresql+asyncpg://literal:literal@literal-host/literal"


def test_database_url_is_required_when_parts_are_incomplete(monkeypatch) -> None:
    monkeypatch.delenv("FINA_DATABASE_URL", raising=False)
    monkeypatch.setenv("FINA_DB_HOST", "db-host")
    monkeypatch.setenv("FINA_DB_USER", "u")
    monkeypatch.setenv("FINA_DB_NAME", "dbname")
    monkeypatch.delenv("FINA_DB_PASSWORD", raising=False)
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    with pytest.raises(ValidationError) as excinfo:
        get_settings()
    assert excinfo.value.errors()[0]["loc"] == ("database_url",)
