from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from fina.domain.audio_analysis import AnalyzeResult, TaskIdMismatchError
from fina.domain.audio_contracts import CallIdentity, CallIdentityMismatchError
from fina.domain.audio_fetch import FetchCallResult
from fina.domain.enums import CallDirection
from fina.repositories.types import AnalysisJobClaim, FetchJobClaim
from fina.services.audio_completion import AnalysisCompletion, FetchCompletion
from tests.analysis_examples import TASK_ID, analysis_data

MANIFEST_KEY = " calls//Звонок +%2F/manifest.json "


@pytest.fixture
def fetch_claim() -> FetchJobClaim:
    return FetchJobClaim(
        call_id=TASK_ID,
        claim_token=uuid4(),
        identity=CallIdentity(
            source_call_id="source-123",
            started_at=datetime(2026, 9, 1, tzinfo=UTC),
            advisor_phone="79990000001",
            counterparty_phone="79990000002",
            call_direction=CallDirection.INBOUND,
        ),
        attempts=1,
    )


@pytest.fixture
def analysis_claim(fetch_claim: FetchJobClaim) -> AnalysisJobClaim:
    return AnalysisJobClaim(
        call_id=fetch_claim.call_id,
        claim_token=fetch_claim.claim_token,
        identity=fetch_claim.identity,
        manifest_object_key=MANIFEST_KEY,
        client_id=uuid4(),
        attempts=fetch_claim.attempts,
    )


def test_fetch_completion_retains_matching_claim_and_exact_manifest_key(
    fetch_claim: FetchJobClaim,
) -> None:
    identity = CallIdentity.model_validate(analysis_data()["identity"])
    # Both the returned identity and the claim normalize the input UTC timestamp.
    assert identity.started_at.isoformat() == "2026-09-01T03:00:00+03:00"
    assert fetch_claim.identity.started_at.isoformat() == "2026-09-01T03:00:00+03:00"
    result = FetchCallResult(identity=identity, manifest_object_key=MANIFEST_KEY)

    completion = FetchCompletion(claim=fetch_claim, result=result)

    assert completion.claim is fetch_claim
    assert completion.result is result
    assert completion.result.manifest_object_key == MANIFEST_KEY


def test_analysis_completion_retains_matching_claim_and_complete_result(
    analysis_claim: AnalysisJobClaim,
) -> None:
    result = AnalyzeResult.model_validate(analysis_data())
    assert result.identity.started_at.isoformat() == "2026-09-01T03:00:00+03:00"

    completion = AnalysisCompletion(claim=analysis_claim, result=result)

    assert completion.claim is analysis_claim
    assert completion.result is result


@pytest.mark.parametrize("kind", ["fetch", "analysis"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_call_id", "another-call"),
        ("started_at", "2026-09-01T00:00:01Z"),
        ("advisor_phone", "79990000003"),
        ("counterparty_phone", "79990000004"),
        ("call_direction", "OUTBOUND"),
    ],
)
def test_completion_construction_rejects_each_identity_mismatch(
    fetch_claim: FetchJobClaim,
    analysis_claim: AnalysisJobClaim,
    kind: str,
    field: str,
    value: str,
) -> None:
    data = analysis_data()
    data["identity"][field] = value
    result = AnalyzeResult.model_validate(data)

    with pytest.raises(CallIdentityMismatchError, match="does not match the queued call") as error:
        if kind == "fetch":
            FetchCompletion(
                claim=fetch_claim,
                result=FetchCallResult(identity=result.identity, manifest_object_key=MANIFEST_KEY),
            )
        else:
            AnalysisCompletion(claim=analysis_claim, result=result)

    assert value not in str(error.value)


def test_analysis_completion_rejects_wrong_task_even_when_identity_matches(
    analysis_claim: AnalysisJobClaim,
) -> None:
    result = AnalyzeResult.model_validate(analysis_data(uuid4()))

    with pytest.raises(TaskIdMismatchError, match="does not match the queued task"):
        AnalysisCompletion(claim=analysis_claim, result=result)


def test_fetch_completion_cannot_be_rebound_to_a_different_call(
    fetch_claim: FetchJobClaim,
) -> None:
    result = FetchCallResult(
        identity=CallIdentity.model_validate(analysis_data()["identity"]),
        manifest_object_key=MANIFEST_KEY,
    )
    completion = FetchCompletion(claim=fetch_claim, result=result)
    other_identity = CallIdentity.model_validate(
        fetch_claim.identity.model_dump() | {"source_call_id": "another-call"}
    )
    other_claim = replace(fetch_claim, identity=other_identity)

    with pytest.raises(CallIdentityMismatchError):
        replace(completion, claim=other_claim)


def test_analysis_completion_cannot_be_rebound_to_a_different_task(
    analysis_claim: AnalysisJobClaim,
) -> None:
    completion = AnalysisCompletion(
        claim=analysis_claim, result=AnalyzeResult.model_validate(analysis_data())
    )
    other_claim = replace(analysis_claim, call_id=uuid4())

    with pytest.raises(TaskIdMismatchError):
        replace(completion, claim=other_claim)
