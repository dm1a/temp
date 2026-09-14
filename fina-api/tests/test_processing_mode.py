import pytest

from fina.processing_mode import ProcessingModeUnavailable, resolve_audio_adapters


def test_api_mode_returns_no_adapters() -> None:
    audio_fetcher, audio_analyzer = resolve_audio_adapters(True)

    assert audio_fetcher is None
    assert audio_analyzer is None


def test_processing_mode_fails_clearly_when_no_real_client_is_available() -> None:
    """No real MTS AudioFetcher / FINA Analyzer SDK client exists yet, so
    turning API mode off must always fail here -- clearly, and before
    any server starts -- rather than silently degrading to API-mode
    behavior or crashing deep inside a worker loop."""

    with pytest.raises(ProcessingModeUnavailable, match="FINA_API_MODE=false"):
        resolve_audio_adapters(False)
