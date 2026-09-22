"""Adapts the external FINA Analyzer SDK to fina's AudioAnalyzer port.

analyze(), client_profile(), and send_order() are all implemented.
"""

import logging
import time
from uuid import UUID

from audio_analyzer import (
    AnalyzeError as RawAnalyzeError,
)
from audio_analyzer import (
    AnalyzeInput as RawAnalyzeInput,
)
from audio_analyzer import (
    AnalyzeResult as RawAnalyzeResult,
)
from audio_analyzer import (
    ClientProfile as RawClientProfile,
)
from audio_analyzer import (
    DialogDescription as RawDialogDescription,
)
from audio_analyzer import (
    ErrorClientProfile as RawErrorClientProfile,
)
from audio_analyzer import (
    ErrorSendOrder as RawErrorSendOrder,
)
from audio_analyzer import (
    FinaAnalyzerClient,
)
from audio_analyzer import (
    OrderType as RawOrderType,
)
from audio_analyzer import (
    SendOrderInput as RawSendOrderInput,
)

from fina.domain.audio_analysis import (
    AggregateProfileError,
    AggregateProfileOutcome,
    AggregateProfileResult,
    AnalysisArtifacts,
    AnalyzeError,
    AnalyzeInput,
    AnalyzeOutcome,
    AnalyzeResult,
    ClientProfile,
    CustomerProfile,
    PreOrder,
    SendOrderError,
    SendOrderOutcome,
    SendOrderResult,
    Transcript,
)
from fina.domain.enums import OrderType

logger = logging.getLogger(__name__)

# Every log in this adapter carries identifiers, outcomes and timings only.
# The provider's payloads (transcripts, summaries, dialog descriptions,
# customer profiles, order details) never reach a log record -- see README's
# "Logging and metrics".


class AudioAnalyzerAdapter:
    """Implements fina's AudioAnalyzer port by wrapping the FINA Analyzer SDK client."""

    def __init__(self, *, client: FinaAnalyzerClient) -> None:
        self._client = client

    async def analyze(self, input_data: AnalyzeInput) -> AnalyzeOutcome:
        started = time.perf_counter()
        try:
            raw = await self._client.analyze(
                task_id=input_data.task_id,
                input_data=RawAnalyzeInput(object_key=input_data.manifest_object_key),
            )
        except Exception:
            logger.exception(
                "analysis provider call raised",
                extra={
                    "task_id": str(input_data.task_id),
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise
        outcome = self.to_outcome(raw)
        context = {"task_id": str(input_data.task_id), "duration_ms": _elapsed_ms(started)}
        if isinstance(outcome, AnalyzeError):
            logger.warning(
                "analysis provider returned an error",
                extra={
                    **context,
                    "error_code": outcome.error_code,
                    "retryable": outcome.retryable,
                },
            )
        else:
            logger.info(
                "analysis provider returned artifacts",
                extra={
                    **context,
                    "segment_count": len(outcome.artifacts.transcript.segments),
                    "has_pre_order": outcome.artifacts.pre_order is not None,
                },
            )
        return outcome

    async def aggregate_client_profile(
        self, *, task_id: UUID, profiles: list[ClientProfile]
    ) -> AggregateProfileOutcome:
        raw_profiles = [RawDialogDescription.model_validate(profile.data) for profile in profiles]
        started = time.perf_counter()
        try:
            raw = await self._client.client_profile(task_id, raw_profiles)
        except Exception:
            logger.exception(
                "client profile provider call raised",
                extra={
                    "task_id": str(task_id),
                    "profile_count": len(raw_profiles),
                    "duration_ms": _elapsed_ms(started),
                },
            )
            raise
        outcome = self.to_aggregate_outcome(raw)
        context = {
            "task_id": str(task_id),
            "profile_count": len(raw_profiles),
            "duration_ms": _elapsed_ms(started),
        }
        if isinstance(outcome, AggregateProfileError):
            logger.warning(
                "client profile provider returned an error",
                extra={
                    **context,
                    "error_code": outcome.error_code,
                    "retryable": outcome.retryable,
                },
            )
        else:
            logger.info("client profile provider returned a profile", extra=context)
        return outcome

    async def send_order(
        self, *, order_id: UUID, client_phone: str, pre_order: PreOrder
    ) -> SendOrderOutcome:
        request = RawSendOrderInput(
            order_id=order_id,
            client_phone_number=_add_plus(client_phone),
            order_type=(
                RawOrderType(pre_order.order_type.value)
                if pre_order.order_type is not None
                else None
            ),
            instrument_name=pre_order.instrument_name,
            volume=pre_order.volume,
            execution_date=pre_order.execution_date,
            price=pre_order.price,
            currency=pre_order.currency or "RUB",
            additional_details=pre_order.additional_details,
        )
        started = time.perf_counter()
        try:
            raw = await self._client.send_order(request)
        except Exception:
            logger.exception(
                "CRM order posting raised",
                extra={"order_id": str(order_id), "duration_ms": _elapsed_ms(started)},
            )
            raise
        outcome = self.to_send_order_outcome(raw)
        context = {"order_id": str(order_id), "duration_ms": _elapsed_ms(started)}
        if isinstance(outcome, SendOrderError):
            logger.warning(
                "CRM order posting returned an error",
                extra={
                    **context,
                    "error_code": outcome.error_code,
                    "retryable": outcome.retryable,
                },
            )
        else:
            logger.info("CRM order posted", extra=context)
        return outcome

    @staticmethod
    def to_outcome(raw: RawAnalyzeResult | RawAnalyzeError) -> AnalyzeOutcome:
        """Translate a real analyze() result into fina's AnalyzeOutcome."""
        if isinstance(raw, RawAnalyzeError):
            return AnalyzeError(
                task_id=raw.task_id,
                error_code=raw.error_code,
                error_message=f"analyzer error: {raw.error_code}",
                retryable=raw.retry,
                http_status=None,
            )

        raw_pre_order = raw.artifacts.pre_order
        return AnalyzeResult(
            task_id=raw.task_id,
            artifacts=AnalysisArtifacts(
                transcript=Transcript(text=raw.artifacts.transcript),
                summary=raw.artifacts.summary or "",
                client_profile=ClientProfile(
                    data=raw.artifacts.dialog_description.model_dump(mode="json")
                ),
                customer_profile=None,
                pre_order=(
                    PreOrder(
                        order_type=(
                            OrderType(raw_pre_order.order_type.value)
                            if raw_pre_order.order_type is not None
                            else None
                        ),
                        instrument_name=raw_pre_order.instrument_name,
                        volume=raw_pre_order.volume,
                        execution_date=raw_pre_order.execution_date,
                        price=raw_pre_order.price,
                        currency=raw_pre_order.currency,
                        additional_details=raw_pre_order.additional_details,
                    )
                    if raw_pre_order is not None
                    else None
                ),
            ),
            processed_at=raw.processed_at,
            provider_result=raw.model_dump(mode="json"),
        )

    @staticmethod
    def to_aggregate_outcome(
        raw: RawClientProfile | RawErrorClientProfile,
    ) -> AggregateProfileOutcome:
        """Translate a real client_profile() result into fina's AggregateProfileOutcome."""
        if isinstance(raw, RawErrorClientProfile):
            return AggregateProfileError(error_code=raw.error_code, retryable=raw.retry)
        return AggregateProfileResult(
            profile=CustomerProfile(data=raw.model_dump(mode="json", exclude={"task_id"}))
        )

    @staticmethod
    def to_send_order_outcome(raw: RawErrorSendOrder | None) -> SendOrderOutcome:
        """Translate a real send_order() result into fina's SendOrderOutcome.

        The SDK signals success with None rather than a distinct result type.
        """
        if raw is None:
            return SendOrderResult()
        return SendOrderError(error_code=raw.error_code, retryable=raw.retry)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _add_plus(phone: str) -> str:
    """fina stores bare digit strings; the Analyzer SDK's phones must be '+'-prefixed E.164."""
    return phone if phone.startswith("+") else f"+{phone}"
