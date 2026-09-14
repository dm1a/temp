from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class RuntimeState:
    """Process-local lifecycle state used by Kubernetes health probes."""

    started: bool = False
    draining: bool = False
    started_at: datetime | None = None
    readiness_failure: str | None = None
