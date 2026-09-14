from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.audio_fetcher import AudioFetcher


class ProcessingModeUnavailable(RuntimeError):
    """Raised at startup when FINA_API_MODE=false is selected but no real
    AudioFetcher/AudioAnalyzer implementation is available to back it."""


def resolve_audio_adapters(
    api_mode: bool,
) -> tuple[AudioFetcher | None, AudioAnalyzer | None]:
    """API mode never runs background workers: audio_fetcher/audio_analyzer
    stay None, matching how this application actually runs today.

    Turning API mode off needs real SDK-backed adapters to drive those
    workers. No real MTS AudioFetcher / FINA Analyzer SDK client exists yet
    -- packages/audio_fetcher and packages/audio_analyzer are local
    stand-ins for the not-yet-published real packages (see their
    docstrings) and cannot back real workers. So FINA_API_MODE=false
    today always fails clearly here, at startup before any port is bound,
    instead of either silently starting with workers disabled or crashing
    deep inside a worker loop the first time it touches a stand-in client.
    """
    if api_mode:
        return None, None

    raise ProcessingModeUnavailable(
        "FINA_API_MODE=false requires real AudioFetcher/AudioAnalyzer SDK "
        "clients, which are not yet implemented -- packages/audio_fetcher "
        "and packages/audio_analyzer are local stand-ins for the "
        "not-yet-published real SDKs. Leave FINA_API_MODE unset (or true) "
        "to run without background workers, or wire real SDK clients into "
        "resolve_audio_adapters() once they exist."
    )
