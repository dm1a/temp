from audio_analyzer import FinaAnalyzerClient, LiteLLMParams, S3Params
from audio_fetcher import MtsAudioFetcherClient
from dishka import AsyncContainer
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from fina.adapters.audio_analyzer_adapter import AudioAnalyzerAdapter
from fina.adapters.audio_fetcher_adapter import AudioFetcherAdapter
from fina.config import ENV_PREFIX, Settings
from fina.services.audio_analyzer import AudioAnalyzer
from fina.services.audio_fetcher import AudioFetcher


class ProcessingModeUnavailable(RuntimeError):
    """Raised at startup when FINA_API_MODE=false is selected but the
    configured AudioFetcher/AudioAnalyzer SDK clients can't be built."""


async def resolve_audio_adapters(
    settings: Settings,
    container: AsyncContainer,
) -> tuple[AudioFetcher | None, AudioAnalyzer | None]:
    """API mode never runs background workers: audio_fetcher/audio_analyzer
    stay None, matching how this application actually runs today.

    Turning API mode off builds real SDK-backed adapters from settings,
    constructing FinaAnalyzerClient/MtsAudioFetcherClient directly --
    packages/audio_fetcher and packages/audio_analyzer are local stand-ins
    for the not-yet-published real packages (see their docstrings): every
    method on those clients raises NotImplementedError if actually called,
    so a worker touching one fails loudly per job rather than silently
    reporting fake success. A setting required to build them but left unset
    fails here too, at startup before any port is bound.
    """
    if settings.api_mode:
        return None, None

    bucket_name, s3_cert_path, llm_url, llm_name, stt_name, llm_cert_path = _require(
        settings,
        "s3_bucket_name",
        "s3_cert_path",
        "llm_url",
        "llm_name",
        "stt_name",
        "llm_cert_path",
    )
    analyzer_client = FinaAnalyzerClient(
        s3_params=S3Params(
            bucket_name=bucket_name,
            s3_access_key=_secret_or_empty(settings.s3_access_key),
            s3_secret_key=settings.s3_secret_key or SecretStr(""),
            s3_endpoint_url=settings.s3_endpoint_url or "",
            s3_cert_path=s3_cert_path,
        ),
        litellm_params=LiteLLMParams(
            llm_url=llm_url,
            llm_key=settings.llm_key or SecretStr(""),
            llm_name=llm_name,
            stt_name=stt_name,
            llm_cert_path=llm_cert_path,
        ),
    )
    fetcher_client = MtsAudioFetcherClient(
        s3_secret_string=settings.s3_secret_key or SecretStr(""),
        mts_secret_string=settings.mts_secret_string or SecretStr(""),
    )

    engine = await container.get(AsyncEngine)
    return (
        AudioFetcherAdapter(engine=engine, client=fetcher_client),
        AudioAnalyzerAdapter(client=analyzer_client),
    )


def _require(settings: Settings, *field_names: str) -> tuple[str, ...]:
    """Fetches each named Settings field by attribute, deriving its env var
    name from ENV_PREFIX instead of a separately hand-maintained string --
    a field renamed here without updating field_names fails loudly via
    getattr, rather than silently reporting a stale env var name."""
    missing = [name for name in field_names if getattr(settings, name) is None]
    if missing:
        env_vars = ", ".join(f"{ENV_PREFIX}{name.upper()}" for name in missing)
        raise ProcessingModeUnavailable(
            f"FINA_API_MODE=false requires {env_vars} to build the FINA Analyzer SDK client"
        )
    return tuple(getattr(settings, name) for name in field_names)


def _secret_or_empty(value: SecretStr | None) -> str:
    return value.get_secret_value() if value is not None else ""
