"""Mirrors the draft FINA Analyzer SDK (final version, as shared 2026-09-22).

The real package is not published or installable yet, so this workspace
package stands in for it: enough of the draft to type-check and unit-test
audio_analyzer_adapter.py against. FinaAnalyzerClient's real implementation
(the actual S3/LiteLLM calls) isn't visible from here, so every method
raises NotImplementedError rather than guessing at real request/response
shapes -- see vault_secrets.client.VaultClient for the same pattern. Delete
this package (and the matching workspace member/dependency entries in the
root pyproject.toml) and depend on the real package once it exists.
"""

import enum
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    NonNegativeInt,
    PositiveInt,
    SecretStr,
    StringConstraints,
)

NotEmptyString = Annotated[str, StringConstraints(min_length=1)]

UTC3 = timezone(timedelta(hours=3))


def enforce_utc3(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() != UTC3.utcoffset(dt):
        raise ValueError("Datetime must be in UTC+3 timezone")
    return dt


Utc3Datetime = Annotated[datetime, AfterValidator(enforce_utc3)]


def enforce_phone_plus(value: str) -> str:
    if not value.startswith("+"):
        raise ValueError("Phone number must start with '+'")
    return value


PhoneNumber = Annotated[str, BeforeValidator(enforce_phone_plus)]


class SpeakerRole(enum.StrEnum):
    ADVISOR = "ADVISOR"
    CLIENT = "CLIENT"
    UNKNOWN = "UNKNOWN"


class OrderType(enum.StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DialogUsefulness(enum.StrEnum):
    USEFUL = "USEFUL"
    NOT_USEFUL = "NOT_USEFUL"


class EmotionalTone(enum.StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ContractAction(enum.StrEnum):
    NOT_DISCUSSED = "NOT_DISCUSSED"
    TOP_UP = "TOP_UP"
    WITHDRAWAL = "WITHDRAWAL"
    TERMINATION = "TERMINATION"
    OTHER = "OTHER"


class RiskAttitude(enum.StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    MODERATE = "MODERATE"
    AGGRESSIVE = "AGGRESSIVE"
    UNKNOWN = "UNKNOWN"


# class CallIdentity(BaseModel):
#     mts_id_call: PositiveInt
#     call_start: Utc3Datetime
#     advisor_phone_number: PhoneNumber
#     client_phone_number: PhoneNumber
#     advisor_is_outbound: bool
#     call_duration_sec: PositiveInt
#     rec_duration_sec: PositiveInt


class AnalyzeInput(BaseModel):
    object_key: str


class TranscriptSegment(BaseModel):
    segment_id: NonNegativeInt
    start_ms: NonNegativeInt
    end_ms: PositiveInt
    speaker: SpeakerRole
    text: str
    # confidence: float = 0.0


class Transcript(BaseModel):
    full_text: NotEmptyString
    language: Literal["ru"] = "ru"
    segments: list[TranscriptSegment] = []


class DialogDescription(BaseModel):
    dialog_theme: NotEmptyString | None
    contract_action: ContractAction = ContractAction.NOT_DISCUSSED
    risk_attitude: RiskAttitude = RiskAttitude.UNKNOWN
    mentioned_assets: list[NotEmptyString] | None = None
    emotional_tone: EmotionalTone
    follow_up_meeting: bool | None
    follow_up_datetime: Utc3Datetime | None
    follow_up_comment: NotEmptyString | None
    usefulness: DialogUsefulness | None = DialogUsefulness.NOT_USEFUL
    advisor_comment: NotEmptyString | None


class PreOrder(BaseModel):
    order_type: OrderType | None = None
    instrument_name: NotEmptyString | None
    volume: NotEmptyString | None
    execution_date: date | None = None
    price: NotEmptyString | None = None
    currency: Literal["RUB"] = "RUB"
    additional_details: NotEmptyString | None


class AnalysisArtifacts(BaseModel):
    transcript: str
    summary: NotEmptyString | None
    dialog_description: DialogDescription
    pre_order: PreOrder | None = None


class AnalyzeResult(BaseModel):
    task_id: UUID
    # call_identity: CallIdentity
    artifacts: AnalysisArtifacts
    processed_at: Utc3Datetime


class AnalyzeError(BaseModel):
    task_id: UUID  # обязательно добавить в логи каждые!!!
    error_code: Literal["internal_error", "network_error"]
    retry: bool = False


class ClientProfile(BaseModel):
    """Aggregated profile returned by client_profile(); echoes the passed task_id."""

    task_id: UUID
    theme_summary: NotEmptyString | None
    contract_summary: ContractAction = ContractAction.NOT_DISCUSSED
    risk_summary: RiskAttitude = RiskAttitude.UNKNOWN
    mentioned_assets_last: list[NotEmptyString] | None = None
    emotional_tone_average: EmotionalTone
    follow_up_meeting_last_one: bool | None
    follow_up_datetime_last_one: Utc3Datetime | None


class ErrorClientProfile(BaseModel):
    task_id: UUID
    error_code: Literal["all_none", "internal_error", "network_error"] = "internal_error"
    retry: bool = False


class SendOrderInput(PreOrder):
    order_id: UUID
    client_phone_number: NotEmptyString


class ErrorSendOrder(BaseModel):
    order_id: UUID
    error_code: Literal["internal_error", "network_error"] = "internal_error"
    retry: bool = False


class LiteLLMParams(BaseModel):
    """Конфигурация LLM для суммаризации и извлечения сущностей."""

    llm_url: str = Field(description="URL LLM API (например, OpenAI-совместимый endpoint)")
    llm_key: SecretStr = Field(
        default=SecretStr(""), description="API ключ для LLM. Пустая строка если не требуется"
    )
    llm_name: str = Field(description="Название модели (например, 'Qwen/Qwen2.5-7B-Instruct')")
    stt_name: str = Field(description="Название модели STT (например, 'gigaam_base')")

    llm_cert_path: str = Field(description="путь до серта")


class S3Params(BaseModel):
    """Расположение аудиофайла в S3.

    Для доступа к аудио используются S3-совместимые credentials.
    """

    bucket_name: str = Field(description="Имя S3-бакета")

    s3_access_key: str = Field(
        default="",
        description="AWS Access Key для доступа к S3. "
        "Пустая строка если используется IAM role или другой механизм аутентификации",
    )
    s3_secret_key: SecretStr = Field(
        default=SecretStr(""),
        description="AWS Secret Key для доступа к S3. "
        "Пустая строка если используется IAM role или другой механизм аутентификации",
    )
    s3_endpoint_url: str = Field(
        default="",
        description="S3 endpoint URL (для S3-совместимых хранилищ: MinIO, Wasabi, etc). "
        "Пустая строка = используется AWS S3 по умолчанию",
    )

    s3_cert_path: str = Field(description="путь до серта")


_NOT_IMPLEMENTED = (
    "FinaAnalyzerClient is a local stand-in for the not-yet-published FINA "
    "Analyzer SDK; it cannot actually analyze calls. Replace it with the "
    "real package before running with FINA_API_MODE=false."
)


class FinaAnalyzerClient:
    """The external FINA Analyzer SDK client (draft), as the adapter consumes it."""

    def __init__(
        self,
        s3_params: S3Params,
        litellm_params: LiteLLMParams,
    ) -> None:
        self.s3_params = s3_params
        self.litellm_params = litellm_params

    async def analyze(
        self,
        task_id: UUID,
        input_data: AnalyzeInput,
    ) -> AnalyzeResult | AnalyzeError:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def client_profile(
        self,
        task_id: UUID,
        input_data: list[DialogDescription],
    ) -> ClientProfile | ErrorClientProfile:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def send_order(
        self,
        input_data: SendOrderInput,
    ) -> ErrorSendOrder | None:
        raise NotImplementedError(_NOT_IMPLEMENTED)
