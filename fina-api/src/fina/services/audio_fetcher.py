from datetime import datetime
from typing import Protocol

from fina.domain.audio_fetch import DiscoveredCall, FetchCallInput, FetchCallOutcome


class AudioFetcher(Protocol):
    """Application port implemented by an adapter around the external fetcher.

    A successful result is returned only after uploading the audio object first
    and the JSON manifest last. Its key must identify that exact manifest.
    The adapter maps the returned identity into FinaAPI's canonical identity and
    translates expected provider failures into FetchCallError.

    Calls may be retried by another replica after an interrupted attempt. The
    implementation must tolerate repeated requests for the same source call.
    """

    async def fetch_call(self, input_data: FetchCallInput) -> FetchCallOutcome: ...

    async def list_available_calls(
        self, *, advisor_phone: str, window_from: datetime, window_to: datetime
    ) -> list[DiscoveredCall]:
        """List calls for one advisor's phone within [window_from, window_to)."""
        ...
