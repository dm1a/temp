import asyncio
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from tests.e2e.data import ADVISOR, CLIENT, START, seed_transcripts
from tests.e2e.stack import AUTH_HEADERS, SERVICES, ComposeStack

pytestmark = pytest.mark.e2e


def test_fresh_migration_and_two_ready_runtime_containers(clean_stack: ComposeStack) -> None:
    stack = clean_stack
    migration = stack.inspect("migrate")
    assert migration["State"]["Status"] == "exited"
    assert migration["State"]["ExitCode"] == 0
    assert migration["Config"]["Cmd"] == ["alembic", "upgrade", "head"]
    assert not any(value.startswith("FINA_MCP_API_KEY=") for value in migration["Config"]["Env"])
    assert stack.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0001_initial_schema"}
    ]
    first, second = [stack.inspect(service) for service in SERVICES]
    assert first["Id"] != second["Id"]
    assert first["Image"] == second["Image"] == migration["Image"]
    assert first["Config"]["User"] == second["Config"]["User"] == "10001:10001"
    with httpx.Client(timeout=5, trust_env=False) as client:
        for url in stack.management_urls.values():
            for probe in ["healthz", "ready"]:
                response = client.get(f"{url}/probes/{probe}")
                assert response.status_code == 204
                assert response.content == b""


@pytest.mark.parametrize(
    "kind,field,search", [("raw", "transcript", "облигация"), ("summarized", "summary", "акция")]
)
def test_transcript_http_contract_on_both_replicas(
    clean_stack: ComposeStack,
    kind: str,
    field: str,
    search: str,
) -> None:
    stack = clean_stack
    asyncio.run(seed_transcripts(stack))
    params = {"advisor_phone": ADVISOR, "client_phone": CLIENT, "limit": 2}
    pages = []
    with httpx.Client(timeout=5, trust_env=False) as client:
        for url in stack.urls.values():
            endpoint = f"{url}/api/v1/transcripts/{kind}"
            for headers in [{}, {"Authorization": "Bearer wrong-key"}]:
                unauthorized = client.get(endpoint, params=params, headers=headers)
                assert unauthorized.status_code == 401
                assert unauthorized.headers["www-authenticate"] == "Bearer"
            response = client.get(endpoint, params=params, headers=AUTH_HEADERS)
            assert response.status_code == 200, response.text
            page = response.json()
            pages.append(page)
            assert [item["call_id"] for item in page["items"]] == [
                str(UUID(int=3)),
                str(UUID(int=2)),
            ]
            assert page["has_next"] is True
            assert page["limit"] == 2 and page["offset"] == 0
            assert page["items"][0]["call_started_at"] == "2026-01-01T14:00:00+03:00"
            assert page["items"][0][field] == (
                "Обсуждаем облигации, запись 3" if kind == "raw" else "Покупка акций, итог 3"
            )
            last = client.get(endpoint, params=params | {"offset": 2}, headers=AUTH_HEADERS)
            assert last.status_code == 200
            assert [item["call_id"] for item in last.json()["items"]] == [str(UUID(int=1))]
            assert last.json()["has_next"] is False
            filtered = client.get(
                endpoint,
                params=params
                | {
                    "search": search,
                    "date_from": (START + timedelta(hours=1)).isoformat(),
                    "date_to": (START + timedelta(hours=2)).isoformat(),
                },
                headers=AUTH_HEADERS,
            )
            assert filtered.status_code == 200
            assert [item["call_id"] for item in filtered.json()["items"]] == [str(UUID(int=2))]
            empty = client.get(
                endpoint, params=params | {"client_phone": "unknown"}, headers=AUTH_HEADERS
            )
            assert empty.status_code == 200 and empty.json()["items"] == []
            invalid = client.get(endpoint, params=params | {"limit": 101}, headers=AUTH_HEADERS)
            assert invalid.status_code == 422
    assert pages[0] == pages[1]


def test_concurrent_discovery_http_requests_enqueue_one_run(clean_stack: ComposeStack) -> None:
    stack = clean_stack

    async def trigger() -> list[httpx.Response]:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            return await asyncio.gather(
                *[
                    client.post(url + "/internal/tasks/discovery")
                    for _ in range(4)
                    for url in stack.management_urls.values()
                ]
            )

    responses = asyncio.run(trigger())
    assert [response.status_code for response in responses] == [204] * 8
    assert all(response.content == b"" for response in responses)
    runs = stack.query("SELECT window_from, window_to, status, claim_token FROM discovery_runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "PENDING"
    assert runs[0]["claim_token"] is None
    assert runs[0]["window_from"] < runs[0]["window_to"]


def test_restart_and_failover_preserve_discovery_boundaries(clean_stack: ComposeStack) -> None:
    stack = clean_stack
    with httpx.Client(timeout=5, trust_env=False) as client:
        assert (
            client.post(stack.management_urls["api-1"] + "/internal/tasks/discovery").status_code
            == 204
        )
        initial = stack.query("SELECT id, window_from, window_to FROM discovery_runs")[0]
        # Supply worker outcomes explicitly; this stack starts without SDK workers.
        stack.query("UPDATE discovery_runs SET status = 'FAILED' WHERE id = $1", initial["id"])
        previous_start = stack.inspect("api-1")["State"]["StartedAt"]
        stack.stop_service("api-1")
        try:
            assert client.get(stack.management_urls["api-2"] + "/probes/ready").status_code == 204
            stack.start_service("api-1")
            assert stack.inspect("api-1")["State"]["StartedAt"] != previous_start
            assert (
                client.post(
                    stack.management_urls["api-1"] + "/internal/tasks/discovery"
                ).status_code
                == 204
            )
            retried = stack.query(
                "SELECT id, window_from, window_to FROM discovery_runs WHERE status = 'PENDING'"
            )[0]
            assert retried["window_from"] == initial["window_from"]
            assert retried["window_to"] > initial["window_to"]
            stack.query(
                "UPDATE discovery_runs SET status = 'COMPLETED' WHERE id = $1", retried["id"]
            )
            stack.stop_service("api-1")
            assert (
                client.post(
                    stack.management_urls["api-2"] + "/internal/tasks/discovery"
                ).status_code
                == 204
            )
            next_run = stack.query(
                "SELECT window_from FROM discovery_runs WHERE status = 'PENDING'"
            )[0]
            assert next_run["window_from"] == retried["window_to"]
        finally:
            stack.start_service("api-1")


def test_database_outage_changes_readiness_and_recovers(clean_stack: ComposeStack) -> None:
    stack = clean_stack
    stack.stop_service("postgres")
    try:
        with httpx.Client(timeout=5, trust_env=False) as client:
            for url in stack.management_urls.values():
                live = client.get(url + "/probes/healthz")
                ready = client.get(url + "/probes/ready")
                assert live.status_code == 204 and live.content == b""
                assert ready.status_code == 503 and ready.content == b""
    finally:
        stack.start_service("postgres")
    assert stack.query("SELECT version_num FROM alembic_version") == [
        {"version_num": "0001_initial_schema"}
    ]
