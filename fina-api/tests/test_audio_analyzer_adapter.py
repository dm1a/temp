import asyncio
from datetime import date, datetime
from uuid import uuid4

from audio_analyzer import AnalysisArtifacts as RawAnalysisArtifacts
from audio_analyzer import AnalyzeError as RawAnalyzeError
from audio_analyzer import AnalyzeResult as RawAnalyzeResult
from audio_analyzer import ClientProfile as RawClientProfile
from audio_analyzer import DialogDescription as RawDialogDescription
from audio_analyzer import EmotionalTone as RawEmotionalTone
from audio_analyzer import ErrorClientProfile as RawErrorClientProfile
from audio_analyzer import ErrorSendOrder as RawErrorSendOrder
from audio_analyzer import OrderType as RawOrderType
from audio_analyzer import PreOrder as RawPreOrder
from audio_analyzer import SendOrderInput as RawSendOrderInput

from fina.adapters.audio_analyzer_adapter import AudioAnalyzerAdapter
from fina.domain.audio_analysis import (
    AggregateProfileError,
    AggregateProfileResult,
    AnalyzeError,
    AnalyzeResult,
    PreOrder,
    SendOrderError,
    SendOrderResult,
)
from fina.domain.clock import APPLICATION_TIMEZONE as UTC3
from fina.domain.enums import OrderType

TASK_ID = uuid4()
PROCESSED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC3)


def _artifacts(*, pre_order: RawPreOrder | None = None) -> RawAnalysisArtifacts:
    return RawAnalysisArtifacts(
        transcript="Обсуждаем облигации",
        summary="Клиент интересуется облигациями.",
        dialog_description=RawDialogDescription(
            dialog_theme="Облигации",
            mentioned_assets=["ОФЗ"],
            emotional_tone=RawEmotionalTone.NEUTRAL,
            follow_up_meeting=False,
            follow_up_datetime=None,
            follow_up_comment=None,
            advisor_comment=None,
        ),
        pre_order=pre_order,
    )


def test_success_maps_transcript_and_profile() -> None:
    raw = RawAnalyzeResult(
        task_id=TASK_ID,
        artifacts=_artifacts(),
        processed_at=PROCESSED_AT,
    )

    outcome = AudioAnalyzerAdapter.to_outcome(raw)

    assert isinstance(outcome, AnalyzeResult)
    assert outcome.task_id == TASK_ID
    assert outcome.artifacts.transcript.text == "Обсуждаем облигации"
    assert outcome.artifacts.transcript.segments == ()
    assert outcome.artifacts.summary == "Клиент интересуется облигациями."
    assert outcome.artifacts.client_profile.data["dialog_theme"] == "Облигации"
    assert outcome.artifacts.customer_profile is None
    assert outcome.artifacts.pre_order is None
    assert outcome.provider_result["task_id"] == str(TASK_ID)


def test_success_with_pre_order_maps_order_fields() -> None:
    raw_pre_order = RawPreOrder(
        order_type=RawOrderType.BUY,
        instrument_name="Сбербанк",
        volume="100 лотов",
        execution_date=date(2026, 9, 30),
        price="Рыночная",
        additional_details="Детали",
    )
    raw = RawAnalyzeResult(
        task_id=TASK_ID,
        artifacts=_artifacts(pre_order=raw_pre_order),
        processed_at=PROCESSED_AT,
    )

    outcome = AudioAnalyzerAdapter.to_outcome(raw)

    assert isinstance(outcome, AnalyzeResult)
    assert outcome.artifacts.pre_order is not None
    assert outcome.artifacts.pre_order.order_type == OrderType.BUY
    assert outcome.artifacts.pre_order.instrument_name == "Сбербанк"
    assert outcome.artifacts.pre_order.volume == "100 лотов"
    assert outcome.artifacts.pre_order.execution_date == date(2026, 9, 30)
    assert outcome.artifacts.pre_order.price == "Рыночная"
    assert outcome.artifacts.pre_order.currency == "RUB"
    assert outcome.artifacts.pre_order.additional_details == "Детали"


def test_error_maps_to_analyze_error_with_retry_flag() -> None:
    raw = RawAnalyzeError(task_id=TASK_ID, error_code="network_error", retry=True)

    outcome = AudioAnalyzerAdapter.to_outcome(raw)

    assert isinstance(outcome, AnalyzeError)
    assert outcome.task_id == TASK_ID
    assert outcome.error_code == "network_error"
    assert outcome.retryable is True
    assert outcome.http_status is None


def test_non_retryable_error_maps_retry_flag_false() -> None:
    raw = RawAnalyzeError(task_id=TASK_ID, error_code="internal_error", retry=False)

    outcome = AudioAnalyzerAdapter.to_outcome(raw)

    assert isinstance(outcome, AnalyzeError)
    assert outcome.retryable is False


def test_aggregate_success_maps_profile_data_and_drops_echoed_task_id() -> None:
    raw = RawClientProfile(
        task_id=TASK_ID,
        theme_summary="Облигации",
        emotional_tone_average=RawEmotionalTone.NEUTRAL,
        follow_up_meeting_last_one=False,
        follow_up_datetime_last_one=None,
    )

    outcome = AudioAnalyzerAdapter.to_aggregate_outcome(raw)

    assert isinstance(outcome, AggregateProfileResult)
    assert outcome.profile.data["theme_summary"] == "Облигации"
    assert "task_id" not in outcome.profile.data


def test_aggregate_error_maps_retry_flag() -> None:
    raw = RawErrorClientProfile(task_id=TASK_ID, error_code="all_none", retry=True)

    outcome = AudioAnalyzerAdapter.to_aggregate_outcome(raw)

    assert isinstance(outcome, AggregateProfileError)
    assert outcome.error_code == "all_none"
    assert outcome.retryable is True


def test_send_order_success_maps_to_result() -> None:
    outcome = AudioAnalyzerAdapter.to_send_order_outcome(None)

    assert isinstance(outcome, SendOrderResult)


def test_send_order_error_maps_retry_flag() -> None:
    raw = RawErrorSendOrder(order_id=uuid4(), error_code="network_error", retry=True)

    outcome = AudioAnalyzerAdapter.to_send_order_outcome(raw)

    assert isinstance(outcome, SendOrderError)
    assert outcome.error_code == "network_error"
    assert outcome.retryable is True


def test_send_order_builds_request_with_plus_prefixed_phone_and_mapped_order_type() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.requests: list[RawSendOrderInput] = []

        async def send_order(self, input_data: RawSendOrderInput) -> None:
            self.requests.append(input_data)
            return None

    async def scenario() -> None:
        client = RecordingClient()
        adapter = AudioAnalyzerAdapter(client=client)
        order_id = uuid4()

        outcome = await adapter.send_order(
            order_id=order_id,
            client_phone="79990000002",
            pre_order=PreOrder(order_type=OrderType.BUY, instrument_name="Сбербанк"),
        )

        assert isinstance(outcome, SendOrderResult)
        assert len(client.requests) == 1
        sent = client.requests[0]
        assert sent.order_id == order_id
        assert sent.client_phone_number == "+79990000002"
        assert sent.order_type == RawOrderType.BUY
        assert sent.instrument_name == "Сбербанк"
        assert sent.currency == "RUB"

    asyncio.run(scenario())
