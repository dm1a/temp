import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from fina.api.probes import ready
from fina.logging_config import JsonFormatter
from fina.metrics import MetricsMiddleware
from fina.runtime import RuntimeState
from tests.conftest import application_client, make_test_application


def test_console_logs_keep_context_without_exception_payloads(caplog):
    logger = logging.getLogger("fina.test")
    try:
        raise ValueError("secret-canary; private transcript; SQL parameters")
    except ValueError:
        logger.exception(
            "provider operation failed",
            extra={
                "worker_id": "replica-a",
                "call_id": "call-123",
                "attempt": 2,
                "payload": "secret-canary",
                "authorization": "secret-canary",
            },
        )

    output = JsonFormatter().format(caplog.records[-1])
    entry = json.loads(output)
    assert entry["worker_id"] == "replica-a"
    assert entry["call_id"] == "call-123"
    assert entry["attempt"] == 2
    assert entry["instance"]
    assert entry["error_type"] == "ValueError"
    assert entry["traceback"][-1]["function"] == (
        "test_console_logs_keep_context_without_exception_payloads"
    )
    assert "secret-canary" not in output
    assert "private transcript" not in output
    assert "SQL parameters" not in output
    assert "\n" not in output


def test_request_logs_use_route_templates_and_keep_routine_probes_quiet(caplog):
    caplog.set_level(logging.INFO, logger="fina.http")

    async def scenario():
        async with application_client(make_test_application()) as client:
            response = await client.get(
                "/api/v1/orders?client_phone=79991234567&token=query-canary",
                headers={"Authorization": "Bearer auth-canary"},
            )
            assert response.status_code == 401
            assert (await client.get("/unmatched-path-canary")).status_code == 404
            await client.get("/probes/ready")
            await client.get("/probes/healthz")
            await client.get("/metrics")

    asyncio.run(scenario())
    records = [record for record in caplog.records if record.name == "fina.http"]
    assert [(record.route, record.status) for record in records] == [
        ("/api/v1/orders", 401),
        ("unmatched", 404),
    ]
    assert all(record.duration_ms >= 0 for record in records)
    output = "\n".join(JsonFormatter().format(record) for record in records)
    for value in ["79991234567", "query-canary", "auth-canary", "unmatched-path-canary"]:
        assert value not in output


def test_readiness_logs_failures_once_and_logs_recovery(caplog):
    caplog.set_level(logging.INFO, logger="fina.api.probes")

    class Probe:
        failing = True

        async def check(self):
            if self.failing:
                raise RuntimeError("database-password-canary")

    async def scenario():
        runtime = RuntimeState(started=True)
        probe = Probe()
        for _ in range(3):
            assert (await ready(runtime, probe)).status_code == 503
        probe.failing = False
        for _ in range(3):
            assert (await ready(runtime, probe)).status_code == 204
        probe.failing = True
        assert (await ready(runtime, probe)).status_code == 503

    asyncio.run(scenario())
    assert [record.getMessage() for record in caplog.records] == [
        "readiness failed",
        "readiness recovered",
        "readiness failed",
    ]
    assert caplog.records[0].reason == "RuntimeError"
    assert "database-password-canary" not in caplog.text


@pytest.mark.parametrize("path", ["/probes/ready", "/metrics", "/api/v1/orders"])
@pytest.mark.parametrize("error_class", [RuntimeError, asyncio.CancelledError])
def test_request_logging_never_swallows_errors_or_cancellation(path, error_class):
    async def app(scope, receive, send):
        raise error_class("error-canary")

    async def scenario():
        middleware = MetricsMiddleware(app)
        scope = {"type": "http", "method": "GET", "route": SimpleNamespace(path=path)}
        with pytest.raises(error_class):
            await middleware(scope, None, None)

    asyncio.run(scenario())
