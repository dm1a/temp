import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from fina.domain.audio_analysis import (
    AnalysisArtifacts,
    AnalyzeError,
    AnalyzeInput,
    AnalyzeOutcome,
    AnalyzeResult,
    ClientProfile,
    PreOrder,
    TaskIdMismatchError,
    Transcript,
    TranscriptSegment,
)
from fina.domain.audio_contracts import CallIdentity
from fina.services.audio_analyzer import AudioAnalyzer
from tests.analysis_examples import TASK_ID, analysis_data


def test_analysis_parses_canonical_task_id_and_shared_call_identity() -> None:
    data = analysis_data()
    queued = CallIdentity.model_validate(data["identity"])
    data["identity"]["started_at"] = "2026-09-01T03:00:00+03:00"
    result = TypeAdapter(AnalyzeOutcome).validate_python(data)

    assert isinstance(result, AnalyzeResult)
    assert result.identity == queued
    assert result.task_id == TASK_ID
    assert result.processed_at.isoformat() == "2026-09-01T03:02:00+03:00"


def test_error_for_wrong_task_cannot_change_retry_state() -> None:
    error = AnalyzeError(task_id=TASK_ID, error_code="TEMPORARY", error_message="", retryable=True)
    error.validate_task(TASK_ID)
    with pytest.raises(TaskIdMismatchError):
        error.validate_task(uuid4())


def test_storage_preserves_complete_provider_data_and_serializes_canonical_dates() -> None:
    data = analysis_data()
    result = AnalyzeResult.model_validate(data)
    stored = result.to_storage()

    assert stored["schema_version"] == "1"
    assert stored["task_id"] == str(TASK_ID)
    assert stored["processed_at"] == "2026-09-01T03:02:00+03:00"
    assert stored["artifacts"]["pre_order"]["execution_date"] == "2026-09-30"
    assert stored["provider_result"] == data["provider_result"]
    assert AnalyzeResult.model_validate(stored) == result
    assert TypeAdapter(AnalyzeOutcome).validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), datetime(2026, 9, 1), uuid4(), b"binary"]
)
def test_provider_payload_requires_json_values_without_lossy_conversion(value: object) -> None:
    data = analysis_data()
    data["provider_result"]["future_metadata"]["value"] = value

    with pytest.raises(ValidationError):
        AnalyzeResult.model_validate(data)


def test_adapter_must_resolve_naive_provider_timestamps_explicitly() -> None:
    data = analysis_data()
    data["processed_at"] = "2026-09-01T00:02:00"
    with pytest.raises(ValidationError, match="timezone"):
        AnalyzeResult.model_validate(data)


@pytest.mark.parametrize("field", ["transcript", "summary", "client_profile"])
def test_incomplete_artifacts_are_not_accepted_as_success(field: str) -> None:
    data = analysis_data()
    del data["artifacts"][field]

    with pytest.raises(ValidationError):
        AnalyzeResult.model_validate(data)


def test_empty_speech_and_unknown_optional_artifacts_do_not_invent_data() -> None:
    artifacts = AnalysisArtifacts(
        transcript=Transcript(text=""), summary="", client_profile=ClientProfile(data={})
    )

    assert artifacts.transcript.segments == ()
    assert artifacts.customer_profile is None
    assert artifacts.pre_order is None
    partial_order = PreOrder(instrument_name="Сбербанк")
    assert partial_order.order_type is None
    assert partial_order.volume is None
    assert partial_order.currency is None


def test_segment_end_cannot_precede_start() -> None:
    with pytest.raises(ValidationError, match="end must not precede"):
        TranscriptSegment(start_seconds=5, end_seconds=2, text="Речь")


@pytest.mark.parametrize("retryable", [True, False])
def test_error_preserves_explicit_retry_decision_without_success_artifacts(retryable: bool) -> None:
    error = TypeAdapter(AnalyzeOutcome).validate_python(
        {
            "kind": "failure",
            "task_id": str(TASK_ID),
            "error_code": "PROVIDER_ERROR",
            "error_message": "",
            "retryable": retryable,
        }
    )

    assert isinstance(error, AnalyzeError)
    error.validate_task(TASK_ID)
    assert error.retryable is retryable
    assert error.http_status is None
    with pytest.raises(ValidationError):
        AnalyzeResult.model_validate(error.model_dump())


@pytest.mark.parametrize("changes", [{}, {"retryable": "false"}])
def test_error_cannot_silently_default_or_coerce_retry_decision(changes: dict) -> None:
    with pytest.raises(ValidationError):
        AnalyzeError.model_validate(
            {"task_id": TASK_ID, "error_code": "UNKNOWN", "error_message": ""} | changes
        )


def test_input_preserves_exact_manifest_key_and_can_use_a_fake_analyzer() -> None:
    key = " calls//Звонок +%2F/manifest.json "
    request = AnalyzeInput(task_id=TASK_ID, manifest_object_key=key)
    data = analysis_data()
    queued = CallIdentity.model_validate(data["identity"])

    class FakeAudioAnalyzer:
        async def analyze(self, input_data: AnalyzeInput) -> AnalyzeOutcome:
            assert input_data.task_id == TASK_ID
            assert input_data.manifest_object_key == key
            return AnalyzeResult.model_validate(data)

    async def scenario(analyzer: AudioAnalyzer) -> None:
        outcome = await analyzer.analyze(request)
        assert isinstance(outcome, AnalyzeResult)
        assert outcome.task_id == request.task_id
        assert outcome.identity == queued

    asyncio.run(scenario(FakeAudioAnalyzer()))
