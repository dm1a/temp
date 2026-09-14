import json
import logging
import socket
import sys
import traceback
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Deliberately exclude payloads, headers, URLs, phones and provider messages.
CONTEXT_FIELDS = (
    "worker_id",
    "worker_kind",
    "call_id",
    "source_call_id",
    "task_id",
    "order_id",
    "client_id",
    "run_id",
    "claim_token",
    "attempt",
    "max_attempts",
    "request_version",
    "count",
    "call_ids",
    "client_ids",
    "run_ids",
    "error_code",
    "error_type",
    "http_status",
    "retryable",
    "operation",
    "duration_ms",
    "method",
    "route",
    "status",
    "reason",
    "worker_count",
    "worker_names",
    "grace_period_seconds",
    "timeout_seconds",
    "advisor_count",
    "call_count",
    "profile_count",
    "segment_count",
    "has_pre_order",
    "order_enqueued",
    "window_from",
    "window_to",
    "expected_revision",
    "found_revision",
)


class JsonFormatter(logging.Formatter):
    """One record per line, with safe context and exception frame locations.

    Exception messages can contain SQL parameters or invalid provider payloads.
    Keep the type and stack locations, without messages, source lines or locals.
    """

    def __init__(self) -> None:
        super().__init__()
        self._instance = socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "instance": self._instance,
            "message": record.getMessage(),
            **{key: getattr(record, key) for key in CONTEXT_FIELDS if hasattr(record, key)},
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["error_type"] = record.exc_info[0].__name__
            entry["traceback"] = [
                {"file": frame.filename, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(record.exc_info[2])
            ]
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """py_logs is a corporate-only package (see Dockerfile vs. Dockerfile.public)
    -- its mere presence, not any setting, decides which logging backend runs.
    Everywhere it isn't installed (local dev, tests, the public build), this
    falls back to console JSON logging.

    Note: the context allowlist and exception filtering below (JsonFormatter)
    only apply to the fallback branch. py_logs is trusted to handle `extra`
    fields and exceptions safely on its own once it's wired in for real --
    revisit this if that trust turns out to be misplaced.
    """
    try:
        import py_logs.logs

        py_logs.logs.init_logging(py_logs.logs.EnvironmentProgramConfigReader())
        logger.info("logging configured via py_logs")
    except ImportError:
        # Explicitly stdout, not StreamHandler()'s stderr default -- the
        # Kubernetes log collector (feeding OpenSearch) picks up stdout.
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        logger.warning("py_logs not installed; falling back to console JSON logging")
