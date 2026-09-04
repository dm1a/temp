from enum import StrEnum


class DiscoveryStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CallDirection(StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class ProcessingDecision(StrEnum):
    PROCESS = "PROCESS"
    SKIP = "SKIP"


class DealType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
