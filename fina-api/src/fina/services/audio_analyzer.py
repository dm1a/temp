from typing import Protocol
from uuid import UUID

from fina.domain.audio_analysis import (
    AggregateProfileOutcome,
    AnalyzeInput,
    AnalyzeOutcome,
    ClientProfile,
    PreOrder,
    SendOrderOutcome,
)


class AudioAnalyzer(Protocol):
    """Application port implemented by an adapter around the external Analyzer.

    The adapter maps manifest_object_key to the provider's object_key and the
    UUID task_id to its expected representation. Returned task IDs and identity
    must come from the provider response, so the worker can check them against
    the queued call. A success includes normalized artifacts and the entire
    provider JSON result. Expected provider errors carry an explicit retry flag.

    Credentials and SDK initialization belong to the adapter, outside these
    per-call models. Repeated requests for the same task can occur after a worker
    is interrupted; PostgreSQL claim tokens fence result commits across replicas.
    """

    async def analyze(self, input_data: AnalyzeInput) -> AnalyzeOutcome: ...

    async def aggregate_client_profile(
        self, *, task_id: UUID, profiles: list[ClientProfile]
    ) -> AggregateProfileOutcome:
        """Aggregate a client's recent per-call profiles into one CustomerProfile.

        `profiles` should be ordered most-recent-call-first. task_id is only
        for traceability/echo-checking against the provider's response, not a
        queued job identity.
        """
        ...

    async def send_order(
        self, *, order_id: UUID, client_phone: str, pre_order: PreOrder
    ) -> SendOrderOutcome:
        """Post a discovered order to CRM Advisory."""
        ...
