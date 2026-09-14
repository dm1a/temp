import asyncio
from datetime import datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from fina.domain.audio_fetch import (
    CallIdentity,
    FetchCallError,
    FetchCallInput,
    FetchCallOutcome,
    FetchCallResult,
)
from fina.domain.enums import CallDirection
from fina.services.audio_fetcher import AudioFetcher


def identity_data(**changes: object) -> dict[str, object]:
    return {
        "source_call_id": "source-123",
        "started_at": "2026-09-01T10:00:00Z",
        "advisor_phone": "79990000001",
        "counterparty_phone": "79990000002",
        "call_direction": "INBOUND",
    } | changes


def test_fetch_result_normalizes_identity_timezone() -> None:
    queued = CallIdentity.model_validate(identity_data())
    returned = CallIdentity.model_validate(identity_data(started_at="2026-09-01T13:00:00+03:00"))
    result = FetchCallResult(identity=returned, manifest_object_key="calls/123/manifest.json")

    assert result.identity == queued
    assert queued.started_at.isoformat() == "2026-09-01T13:00:00+03:00"


def test_call_identity_rejects_an_ambiguous_naive_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        CallIdentity.model_validate(identity_data(started_at=datetime(2026, 9, 1, 10)))


def test_source_ids_and_manifest_keys_are_preserved_exactly() -> None:
    source_id = " source/+%2F/001 "
    # An object key is opaque. Spaces, repeated slashes and percent signs must survive mapping.
    key = " recordings//Звонок +%2F/manifest.json "
    request = FetchCallInput(source_call_id=source_id)
    result = FetchCallResult(
        identity=CallIdentity.model_validate(identity_data(source_call_id=source_id)),
        manifest_object_key=key,
    )

    decoded = TypeAdapter(FetchCallOutcome).validate_json(result.model_dump_json())

    assert request.source_call_id == source_id
    assert isinstance(decoded, FetchCallResult)
    assert decoded.identity.source_call_id == source_id
    assert decoded.manifest_object_key == key


@pytest.mark.parametrize("value", ["", " \t\n", 123])
def test_fetch_contract_rejects_invalid_source_ids_and_manifest_keys(value: object) -> None:
    with pytest.raises(ValidationError):
        FetchCallInput.model_validate({"source_call_id": value})
    with pytest.raises(ValidationError):
        FetchCallResult.model_validate({"identity": identity_data(), "manifest_object_key": value})


def test_provider_fields_require_explicit_mapping() -> None:
    # SDK names must be translated by the adapter; they cannot silently bypass validation.
    with pytest.raises(ValidationError):
        FetchCallInput.model_validate({"id_call": "source-123"})
    with pytest.raises(ValidationError):
        FetchCallResult.model_validate(
            {
                "identity": identity_data(),
                "manifest_object_key": "calls/123/manifest.json",
                "object_key": "calls/123/audio.wav",
            }
        )


@pytest.mark.parametrize("retryable", [True, False])
def test_failure_is_distinct_from_success_and_preserves_retry_decision(retryable: bool) -> None:
    outcome = TypeAdapter(FetchCallOutcome).validate_python(
        {
            "kind": "failure",
            "error_code": "PROVIDER_ERROR",
            "error_message": "",
            "retryable": retryable,
        }
    )

    assert isinstance(outcome, FetchCallError)
    assert outcome.retryable is retryable
    assert outcome.http_status is None
    with pytest.raises(ValidationError):
        FetchCallResult.model_validate(outcome.model_dump())


@pytest.mark.parametrize("changes", [{}, {"retryable": "false"}, {"retryable": 1}])
def test_adapter_must_supply_an_explicit_boolean_retry_decision(changes: dict) -> None:
    with pytest.raises(ValidationError):
        FetchCallError.model_validate(
            {"error_code": "PROVIDER_ERROR", "error_message": "failure"} | changes
        )


def test_fetcher_can_be_implemented_without_the_external_sdk() -> None:
    queued = CallIdentity.model_validate(identity_data())

    class FakeAudioFetcher:
        async def fetch_call(self, input_data: FetchCallInput) -> FetchCallOutcome:
            assert input_data.source_call_id == queued.source_call_id
            return FetchCallResult(identity=queued, manifest_object_key="calls/123/manifest.json")

    async def scenario(fetcher: AudioFetcher) -> None:
        outcome = await fetcher.fetch_call(FetchCallInput(source_call_id=queued.source_call_id))
        assert isinstance(outcome, FetchCallResult)
        assert outcome.identity == queued
        assert outcome.identity.call_direction is CallDirection.INBOUND

    asyncio.run(scenario(FakeAudioFetcher()))
