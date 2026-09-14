"""Adapts the external MTS AudioFetcher SDK to fina's AudioFetcher port.

fetch_call() only receives a source_call_id (see FetchCallInput); the MTS
SDK's fetch_and_store() also needs mts_filename, call durations, and
phone/direction data that fina recorded at discovery time. Rather than
widening FetchCallInput with MTS-specific fields (fina's port is meant to
stay provider-agnostic -- see test_provider_fields_require_explicit_mapping),
this adapter looks that data up itself from the calls table.
"""

import logging
import time
from datetime import datetime

from audio_fetcher import (
    AudioFetchError,
    AudioFetchRequest,
    AudioFetchResponse,
    AudioMetaFetchItem,
    AudioMetaFetchRequest,
    MtsAudioFetcherClient,
)
from sqlalchemy.ext.asyncio import AsyncEngine

from fina.domain.audio_contracts import CallIdentity
from fina.domain.audio_fetch import (
    DiscoveredCall,
    FetchCallError,
    FetchCallInput,
    FetchCallOutcome,
    FetchCallResult,
)
from fina.domain.clock import to_application_timezone
from fina.domain.enums import CallDirection
from fina.repositories.calls import CallRepository

logger = logging.getLogger(__name__)


class AudioFetcherAdapter:
    """Implements fina's AudioFetcher port by wrapping the MTS SDK client."""

    def __init__(self, *, engine: AsyncEngine, client: MtsAudioFetcherClient) -> None:
        self._engine = engine
        self._client = client

    async def fetch_call(self, input_data: FetchCallInput) -> FetchCallOutcome:
        async with self._engine.connect() as connection:
            call = await CallRepository(connection).get_by_source_call_id(input_data.source_call_id)
        if call is None:
            logger.error(
                "no calls row for the queued fetch job; cannot build a provider request",
                extra={"source_call_id": input_data.source_call_id},
            )
            raise LookupError(
                f"No calls row for source_call_id={input_data.source_call_id!r}; "
                "cannot build an AudioFetchRequest"
            )

        request = AudioFetchRequest(
            task_id=call.id,
            call_id=int(call.source_call_id),
            advisor_phone=_add_plus(call.advisor_phone),
            client_phone=_add_plus(call.counterparty_phone),
            advisor_is_outbound=call.call_direction == CallDirection.OUTBOUND,
            call_start_dt=to_application_timezone(call.started_at),
            call_duration_sec=call.call_duration_sec,
            rec_duration_sec=call.rec_duration_sec,
            mts_filename=call.mts_filename,
        )
        started = time.perf_counter()
        try:
            raw = await self._client.fetch_and_store(request)
        except Exception:
            # The worker turns this into a retry; without a log here the
            # provider call itself leaves no trace of having been attempted.
            logger.exception(
                "audio fetch provider call raised",
                extra={
                    "call_id": str(call.id),
                    "source_call_id": input_data.source_call_id,
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise
        outcome = self.to_outcome(raw)
        context = {
            "call_id": str(call.id),
            "source_call_id": input_data.source_call_id,
            "duration_ms": _elapsed_ms(started),
        }
        if isinstance(outcome, FetchCallError):
            logger.warning(
                "audio fetch provider returned an error",
                extra={
                    **context,
                    "error_code": outcome.error_code,
                    "http_status": outcome.http_status,
                    "retryable": outcome.retryable,
                },
            )
        else:
            logger.info("audio fetch provider returned audio", extra=context)
        return outcome

    async def list_available_calls(
        self, *, advisor_phone: str, window_from: datetime, window_to: datetime
    ) -> list[DiscoveredCall]:
        request = AudioMetaFetchRequest(
            start_range=to_application_timezone(window_from),
            end_range=to_application_timezone(window_to),
            advisor_phone=_add_plus(advisor_phone),
        )
        started = time.perf_counter()
        try:
            items = await self._client.list_available_calls(request)
        except Exception:
            logger.exception(
                "call listing provider call raised",
                extra={
                    "window_from": window_from.isoformat(),
                    "window_to": window_to.isoformat(),
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise
        # No advisor_phone here, or anywhere else in these logs: it is
        # personal data, and the window plus the count is what an operator
        # actually needs to explain a discovery run's size.
        logger.info(
            "call listing provider returned calls",
            extra={
                "window_from": window_from.isoformat(),
                "window_to": window_to.isoformat(),
                "call_count": len(items),
                "duration_ms": _elapsed_ms(started),
            },
        )
        return [self.to_discovered_call(item) for item in items]

    @staticmethod
    def to_outcome(raw: AudioFetchResponse | AudioFetchError) -> FetchCallOutcome:
        """Translate a real fetch_and_store() result into fina's FetchCallOutcome."""
        if isinstance(raw, AudioFetchError):
            return FetchCallError(
                error_code=str(raw.status_code),
                error_message=raw.error_message or "",
                retryable=raw.need_retry,
                http_status=raw.status_code,
            )
        return FetchCallResult(
            identity=CallIdentity(
                source_call_id=str(raw.call_id),
                started_at=raw.call_start_dt,
                advisor_phone=_strip_plus(raw.advisor_phone),
                counterparty_phone=_strip_plus(raw.client_phone),
                call_direction=(
                    CallDirection.OUTBOUND if raw.advisor_is_outbound else CallDirection.INBOUND
                ),
            ),
            manifest_object_key=raw.object_key,
        )

    @staticmethod
    def to_discovered_call(item: AudioMetaFetchItem) -> DiscoveredCall:
        """Translate one real list_available_calls() item into a DiscoveredCall."""
        return DiscoveredCall(
            identity=CallIdentity(
                source_call_id=str(item.call_id),
                started_at=item.call_start,
                advisor_phone=_strip_plus(item.advisor_phone),
                counterparty_phone=_strip_plus(item.client_phone),
                call_direction=(
                    CallDirection.OUTBOUND if item.advisor_is_outbound else CallDirection.INBOUND
                ),
            ),
            mts_filename=item.mts_filename,
            call_duration_sec=item.call_duration_sec,
            rec_duration_sec=item.rec_duration_sec,
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _strip_plus(phone: str) -> str:
    """MTS phones are '+'-prefixed E.164; fina stores bare digit strings."""
    return phone.removeprefix("+")


def _add_plus(phone: str) -> str:
    """fina stores bare digit strings; MTS phones must be '+'-prefixed E.164."""
    return phone if phone.startswith("+") else f"+{phone}"
