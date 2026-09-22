import asyncio
from unittest.mock import AsyncMock

import pytest
from dishka import AsyncContainer
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from fina.adapters.audio_analyzer_adapter import AudioAnalyzerAdapter
from fina.adapters.audio_fetcher_adapter import AudioFetcherAdapter
from fina.config import Settings
from fina.processing_mode import ProcessingModeUnavailable, resolve_audio_adapters


def _run(settings: Settings, container: AsyncContainer) -> tuple[object, object]:
    return asyncio.run(resolve_audio_adapters(settings, container))


def test_api_mode_returns_no_adapters() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://unused/db",
        mcp_api_key=SecretStr("test"),
        api_mode=True,
    )

    audio_fetcher, audio_analyzer = _run(settings, AsyncMock())

    assert audio_fetcher is None
    assert audio_analyzer is None


def test_worker_mode_builds_adapters_from_complete_settings() -> None:
    container = AsyncMock()
    container.get.return_value = AsyncMock(spec=AsyncEngine)
    settings = Settings(
        database_url="postgresql+asyncpg://unused/db",
        mcp_api_key=SecretStr("test"),
        api_mode=False,
        s3_bucket_name="bucket",
        s3_cert_path="/certs/s3.pem",
        llm_url="https://llm.internal",
        llm_name="Qwen/Qwen2.5-7B-Instruct",
        stt_name="gigaam_base",
        llm_cert_path="/certs/llm.pem",
    )

    audio_fetcher, audio_analyzer = _run(settings, container)

    assert isinstance(audio_fetcher, AudioFetcherAdapter)
    assert isinstance(audio_analyzer, AudioAnalyzerAdapter)


def test_worker_mode_fails_clearly_when_sdk_config_is_missing() -> None:
    """No real MTS AudioFetcher / FINA Analyzer SDK client exists yet -- the
    stand-in's methods all raise NotImplementedError if actually called --
    but a missing setting needed just to build the client must still fail
    here, clearly, before any server starts."""

    settings = Settings(
        database_url="postgresql+asyncpg://unused/db",
        mcp_api_key=SecretStr("test"),
        api_mode=False,
    )

    with pytest.raises(ProcessingModeUnavailable, match="FINA_API_MODE=false"):
        _run(settings, AsyncMock())
