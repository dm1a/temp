"""Startup, shutdown, and crash-recovery behavior against the real two-replica
Compose stack -- complementing test_application.py's HTTP-contract coverage."""

import asyncio
import subprocess
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fina.domain.audio_analysis import AnalyzeResult
from fina.domain.audio_contracts import CallIdentity
from fina.domain.audio_fetch import FetchCallResult
from fina.domain.clock import SystemClock
from fina.domain.enums import CallDirection
from fina.repositories.fetch_jobs import FetchJobRepository
from fina.services.analysis_worker import AnalysisWorker
from fina.services.fetch_worker import FetchWorker
from fina.services.worker_loop import generate_worker_id
from tests.analysis_examples import analysis_data
from tests.e2e.stack import AUTH_HEADERS, ComposeStack
from tests.integration.helpers import ADVISOR, CLIENT, START, seed_call

pytestmark = pytest.mark.e2e

SOURCE_ID = "source-123"  # matches tests.analysis_examples.analysis_data's identity


class FakeAudioFetcher:
    def __init__(self, result: FetchCallResult) -> None:
        self._result = result

    async def fetch_call(self, input_data):
        return self._result

    async def list_available_calls(self, **kwargs):
        return []


class FakeAudioAnalyzer:
    def __init__(self, result) -> None:
        self._result = result

    async def analyze(self, input_data):
        return self._result


def test_missing_required_settings_fails_fast(clean_stack: ComposeStack) -> None:
    """No FINA_DATABASE_URL/FINA_MCP_API_KEY at all: Settings() must reject
    the process before it ever tries to bind a port or reach the database."""
    result = clean_stack.run_one_off("python", "-m", "fina")
    assert result.returncode != 0
    combined = (result.stdout + result.stderr).lower()
    assert "validationerror" in combined or "database_url" in combined


def test_container_request_logs_include_context_without_sensitive_inputs(clean_stack):
    """Only checks secret absence, not log structure: this container logs
    through py_logs (see fina.logging_config.configure_logging), which ships
    structured fields to Logstash rather than rendering them as JSON on
    stdout the way the local JsonFormatter fallback does -- route/status/
    duration_ms aren't observable from `docker compose logs` here. The
    secret-redaction property itself is still exactly what we can and must
    verify from stdout."""
    with httpx.Client(timeout=5, trust_env=False) as client:
        response = client.get(
            clean_stack.urls["api-1"] + "/api/v1/orders",
            params={"client_phone": "79997654321", "token": "query-secret-canary"},
            headers={"Authorization": "Bearer header-secret-canary"},
        )
        assert response.status_code == 401

    logs = clean_stack.compose("logs", "--no-color", "--no-log-prefix", "api-1")
    for secret in ["79997654321", "query-secret-canary", "header-secret-canary"]:
        assert secret not in logs


@pytest.mark.parametrize(
    ("env", "missing_field", "secret"),
    [
        ({"FINA_MCP_API_KEY": "api-key-canary"}, "database_url", "api-key-canary"),
        (
            {"FINA_DATABASE_URL": "postgresql+asyncpg://user:db-password-canary@localhost/db"},
            "mcp_api_key",
            "db-password-canary",
        ),
    ],
)
def test_startup_errors_do_not_log_secrets(clean_stack, env, missing_field, secret) -> None:
    result = clean_stack.run_one_off("python", "-m", "fina", env=env)
    assert result.returncode != 0
    logs = result.stdout + result.stderr
    assert missing_field in logs
    assert secret not in logs


def test_processing_mode_fails_fast(clean_stack: ComposeStack) -> None:
    """FINA_API_MODE=false has no real AudioFetcher/AudioAnalyzer to run yet
    (fina.processing_mode) -- it must fail loudly before binding any port,
    not silently fall back to API-only behavior."""
    result = clean_stack.run_one_off(
        "python",
        "-m",
        "fina",
        env={
            "FINA_DATABASE_URL": "postgresql+asyncpg://unused:unused@localhost/unused",
            "FINA_MCP_API_KEY": "unused",
            "FINA_API_MODE": "false",
        },
    )
    assert result.returncode != 0
    # The exception *message* (which names AudioFetcher/AudioAnalyzer) is
    # deliberately never logged -- JsonFormatter keeps only the exception
    # type and traceback locations. Assert on what's actually emitted.
    assert "processingmodeunavailable" in (result.stdout + result.stderr).lower()


def test_graceful_shutdown_exits_cleanly_within_grace_period(clean_stack: ComposeStack) -> None:
    """SIGTERM (docker stop's default signal) must be handled: the process
    drains and exits on its own, well before Docker would give up and
    SIGKILL it. See fina.lifespan's SHUTDOWN_GRACE_PERIOD (40s)."""
    stack = clean_stack
    try:
        container_id = stack.compose("ps", "--quiet", "api-1")
        started = time.monotonic()
        subprocess.run(["docker", "stop", "--time", "55", container_id], check=True, timeout=60)
        elapsed = time.monotonic() - started
        info = stack.inspect("api-1")
        assert info["State"]["ExitCode"] == 0
        # Comfortably below the 55s forced-kill deadline -- proves the
        # process exited on its own instead of being SIGKILLed after the
        # full timeout elapsed.
        assert elapsed < 50
    finally:
        stack.start_service("api-1")


@pytest.mark.parametrize("port", [8000, 9000])
def test_shutdown_cancels_a_hanging_http_request(clean_stack: ComposeStack, port: int) -> None:
    script = Path(__file__).with_name("slow_request_shutdown.py").read_text()
    result = clean_stack.run_one_off(
        "python",
        "-c",
        script,
        str(port),
        env={
            "FINA_DATABASE_URL": "postgresql+asyncpg://unused:unused@localhost/unused",
            "FINA_MCP_API_KEY": "unused",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "active request cancelled; shutdown completed" in result.stdout


def test_forced_termination_other_replica_stays_ready(clean_stack: ComposeStack) -> None:
    """A hard crash (SIGKILL, no chance to drain) of one replica must not
    affect the other's readiness, and the killed replica must come back
    cleanly once restarted."""
    stack = clean_stack
    try:
        container_id = stack.compose("ps", "--quiet", "api-1")
        subprocess.run(["docker", "kill", "-s", "SIGKILL", container_id], check=True, timeout=15)
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(stack.management_urls["api-2"] + "/probes/ready")
            assert response.status_code == 204
    finally:
        stack.start_service("api-1")
    with httpx.Client(timeout=5, trust_env=False) as client:
        assert client.get(stack.management_urls["api-1"] + "/probes/ready").status_code == 204


def test_database_outage_readiness_recovers_after_restart(clean_stack: ComposeStack) -> None:
    """Extends test_application.py's outage test with the other half of the
    story: readiness must come back once the database does, not just report
    correctly while it's down."""
    stack = clean_stack
    stack.stop_service("postgres")
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for url in stack.management_urls.values():
                assert client.get(url + "/probes/ready").status_code == 503
    finally:
        stack.start_service("postgres")
    deadline = time.monotonic() + 15
    with httpx.Client(timeout=5, trust_env=False) as client:
        for url in stack.management_urls.values():
            while True:
                response = client.get(url + "/probes/ready")
                if response.status_code == 204:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{url}/probes/ready did not recover in time")
                time.sleep(0.2)


def test_call_processing_via_workers_recovers_stale_claim_and_is_visible_on_both_replicas(
    clean_stack: ComposeStack,
) -> None:
    """Drives a call through the real FetchWorker/AnalysisWorker pipeline --
    including recovering an abandoned claim, exactly as a crashed worker
    replica would leave one -- instead of inserting a COMPLETED row
    directly. The two live API containers only ever see what these
    out-of-process workers write to the one shared database, proving that
    shared state (not a lucky same-process shortcut) is what makes the
    result visible on both replicas.

    Worker loops aren't deployed in these containers yet (see README's
    "Container deployment"); running the real worker classes here, against
    the same database the containers use, is the test-integration
    equivalent until they are.
    """
    stack = clean_stack

    async def scenario() -> None:
        engine = create_async_engine(stack.sqlalchemy_database_url())
        try:
            async with engine.connect() as connection, connection.begin():
                call_id = await seed_call(connection, SOURCE_ID)

            # A worker claims the fetch job, then "crashes" without
            # completing it -- nothing marks it FAILED or retries it; the
            # claim is simply abandoned, as a killed process would leave it.
            async with engine.connect() as connection, connection.begin():
                crashed_claim = await FetchJobRepository(connection).claim_next(
                    worker_id="crashed-worker"
                )
                assert crashed_claim is not None
                assert crashed_claim.call_id == call_id
            async with engine.connect() as connection, connection.begin():
                await connection.execute(
                    text(
                        "UPDATE audio_fetch_jobs SET locked_at = locked_at - INTERVAL '1 hour' "
                        "WHERE call_id = :call_id"
                    ),
                    {"call_id": call_id},
                )

            identity = CallIdentity(
                source_call_id=SOURCE_ID,
                started_at=START,
                advisor_phone=ADVISOR,
                counterparty_phone=CLIENT,
                call_direction=CallDirection.INBOUND,
            )
            fetch_result = FetchCallResult(
                identity=identity,
                manifest_object_key=f"e2e/pipeline/{call_id}",
            )
            fetch_worker = FetchWorker(
                engine=engine,
                fetcher=FakeAudioFetcher(fetch_result),
                clock=SystemClock(),
                worker_id=generate_worker_id(),
                stale_after=timedelta(seconds=0),
            )
            await fetch_worker._recover_stale()  # a second replica's poll would do this too
            assert await fetch_worker.run_once() is True

            analysis_result = AnalyzeResult.model_validate(analysis_data(call_id))
            analysis_worker = AnalysisWorker(
                engine=engine,
                analyzer=FakeAudioAnalyzer(analysis_result),
                clock=SystemClock(),
                worker_id=generate_worker_id(),
            )
            assert await analysis_worker.run_once() is True
        finally:
            await engine.dispose()

    asyncio.run(scenario())

    params = {"advisor_phone": ADVISOR, "client_phone": CLIENT, "limit": 10}
    with httpx.Client(timeout=5, trust_env=False) as client:
        for url in stack.urls.values():
            response = client.get(
                f"{url}/api/v1/transcripts/raw", params=params, headers=AUTH_HEADERS
            )
            assert response.status_code == 200, response.text
            items = response.json()["items"]
            assert len(items) == 1
            assert items[0]["transcript"] == "Обсуждаем облигации и акции."
