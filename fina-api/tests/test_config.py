import pytest
from pydantic import ValidationError
from vault_secrets import _vault_tls_verify

from fina.config import DatabaseSettings, SecretSettings, Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_settings_reads_documented_environment_variables(monkeypatch) -> None:
    """Locks in the FINA_-prefixed env var names documented in README.md and
    .env.example.

    get_settings() is the only call site that parses real process environment
    variables (every test elsewhere constructs Settings(...) via kwargs), so this
    is the one place a container-breaking rename like a prefix drift would
    otherwise go uncaught outside a full `--run-e2e` Docker run.
    """

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
    """API-only is the explicit, safe default -- matching how this
    application actually runs today, everywhere FINA_API_MODE isn't set."""

    monkeypatch.delenv("FINA_API_MODE", raising=False)
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert settings.api_mode is True


def test_unprefixed_environment_variables_are_ignored(monkeypatch) -> None:
    """Only the FINA_-prefixed names are read -- a bare DATABASE_URL (the
    convention before this prefix was introduced) must not leak in."""

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


@pytest.mark.parametrize("model", [Settings, SecretSettings, DatabaseSettings])
def test_settings_errors_hide_unparsed_input(model) -> None:
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({"mcp_api_key": {"secret": "private-value-canary"}})
    assert "private-value-canary" not in str(excinfo.value)


def test_startup_without_vault_reads_mcp_api_key_from_env(monkeypatch) -> None:
    """Settings must behave exactly like plain env-var loading whenever
    VAULT_URL is unset -- i.e. everywhere outside the corporate network,
    including every other test in this suite."""

    monkeypatch.delenv("VAULT_URL", raising=False)
    monkeypatch.setenv("FINA_DATABASE_URL", "postgresql+asyncpg://u:p@db-host:5432/dbname")
    monkeypatch.setenv("FINA_MCP_API_KEY", "env-provided-key")

    settings = get_settings()

    assert settings.mcp_api_key.get_secret_value() == "env-provided-key"


# test_vault_secrets_supplies_settings_when_vault_url_is_set and
# test_vault_client_stand_in_raises_if_ever_actually_invoked were removed
# after packages/vault_secrets (the local stand-in) was deleted in favor of
# the real corporate vault-secrets package: the first monkeypatched the
# stand-in's VaultAuthService and needs rewriting against the real package's
# actual API; the second tested the stand-in's NotImplementedError guard,
# which no longer exists. TODO: reinstate Vault-wiring coverage against the
# real package's API.


def test_vault_tls_verify_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("VAULT_CA_BUNDLE", raising=False)
    assert _vault_tls_verify() is True


def test_vault_tls_verify_false_disables_verification(monkeypatch) -> None:
    monkeypatch.setenv("VAULT_CA_BUNDLE", "false")
    assert _vault_tls_verify() is False


def test_vault_tls_verify_false_is_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("VAULT_CA_BUNDLE", "FALSE")
    assert _vault_tls_verify() is False


def test_vault_tls_verify_path_is_passed_through(monkeypatch) -> None:
    monkeypatch.setenv("VAULT_CA_BUNDLE", "/etc/ssl/corp-ca-bundle.pem")
    assert _vault_tls_verify() == "/etc/ssl/corp-ca-bundle.pem"
