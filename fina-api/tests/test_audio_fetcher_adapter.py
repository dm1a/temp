import uuid
from datetime import datetime

from audio_fetcher import (
    AudioFetchError,
    AudioFetchResponse,
    AudioMetaFetchItem,
)

from fina.adapters.audio_fetcher_adapter import AudioFetcherAdapter
from fina.domain.audio_fetch import DiscoveredCall, FetchCallError, FetchCallResult
from fina.domain.clock import APPLICATION_TIMEZONE as UTC3
from fina.domain.enums import CallDirection

TASK_ID = uuid.uuid4()


def test_outbound_success_maps_identity_and_object_key() -> None:
    raw = AudioFetchResponse(
        task_id=TASK_ID,
        call_id=12345678,
        advisor_phone="+79990000001",
        client_phone="+79990000002",
        advisor_is_outbound=True,
        call_start_dt=datetime(2026, 9, 1, 12, 0, tzinfo=UTC3),
        call_duration_sec=120,
        rec_duration_sec=118,
        mts_filename="rec.mp3",
        object_key="calls/12345678.mp3",
        status_code=200,
    )

    outcome = AudioFetcherAdapter.to_outcome(raw)

    assert isinstance(outcome, FetchCallResult)
    assert outcome.identity.source_call_id == "12345678"
    assert outcome.identity.started_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC3)
    assert outcome.identity.advisor_phone == "79990000001"
    assert outcome.identity.counterparty_phone == "79990000002"
    assert outcome.identity.call_direction == CallDirection.OUTBOUND
    assert outcome.manifest_object_key == "calls/12345678.mp3"


def test_inbound_call_maps_direction() -> None:
    raw = AudioFetchResponse(
        task_id=TASK_ID,
        call_id=1,
        advisor_phone="+79990000001",
        client_phone="+79990000002",
        advisor_is_outbound=False,
        call_start_dt=datetime(2026, 9, 1, tzinfo=UTC3),
        call_duration_sec=1,
        rec_duration_sec=1,
        mts_filename="rec.mp3",
        object_key="calls/1.mp3",
        status_code=200,
    )

    outcome = AudioFetcherAdapter.to_outcome(raw)

    assert isinstance(outcome, FetchCallResult)
    assert outcome.identity.call_direction == CallDirection.INBOUND


def test_error_response_maps_to_fetch_call_error() -> None:
    raw = AudioFetchError(
        task_id=TASK_ID,
        call_id=12345678,
        status_code=404,
        error_message="not found",
        need_retry=False,
    )

    outcome = AudioFetcherAdapter.to_outcome(raw)

    assert isinstance(outcome, FetchCallError)
    assert outcome.error_code == "404"
    assert outcome.error_message == "not found"
    assert outcome.retryable is False
    assert outcome.http_status == 404


def test_error_with_no_message_maps_to_empty_string_and_retryable() -> None:
    raw = AudioFetchError(
        task_id=TASK_ID,
        call_id=1,
        status_code=500,
        error_message=None,
        need_retry=True,
    )

    outcome = AudioFetcherAdapter.to_outcome(raw)

    assert isinstance(outcome, FetchCallError)
    assert outcome.error_message == ""
    assert outcome.retryable is True


def test_discovered_item_maps_identity_and_mts_fields() -> None:
    item = AudioMetaFetchItem(
        advisor_phone="+79990000001",
        client_phone="+79990000002",
        advisor_is_outbound=True,
        call_id=12345678,
        call_start=datetime(2026, 9, 1, 12, 0, tzinfo=UTC3),
        mts_filename="rec.mp3",
        call_duration_sec=90,
        rec_duration_sec=88,
    )

    discovered = AudioFetcherAdapter.to_discovered_call(item)

    assert isinstance(discovered, DiscoveredCall)
    assert discovered.identity.source_call_id == "12345678"
    assert discovered.identity.started_at == datetime(2026, 9, 1, 12, 0, tzinfo=UTC3)
    assert discovered.identity.advisor_phone == "79990000001"
    assert discovered.identity.counterparty_phone == "79990000002"
    assert discovered.identity.call_direction == CallDirection.OUTBOUND
    assert discovered.mts_filename == "rec.mp3"
    assert discovered.call_duration_sec == 90
    assert discovered.rec_duration_sec == 88


def test_discovered_item_inbound_maps_direction() -> None:
    item = AudioMetaFetchItem(
        advisor_phone="+79990000001",
        client_phone="+79990000002",
        advisor_is_outbound=False,
        call_id=1,
        call_start=datetime(2026, 9, 1, tzinfo=UTC3),
        mts_filename="rec.mp3",
        call_duration_sec=1,
        rec_duration_sec=1,
    )

    discovered = AudioFetcherAdapter.to_discovered_call(item)

    assert discovered.identity.call_direction == CallDirection.INBOUND
