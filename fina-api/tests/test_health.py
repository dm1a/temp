import asyncio

from fina.runtime import RuntimeState
from tests.conftest import application_client, make_test_application


def test_probes_when_application_is_ready() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            for path in ("/probes/healthz", "/probes/ready"):
                response = await client.get(path)
                assert response.status_code == 204
                assert response.content == b""

    asyncio.run(scenario())


def test_readiness_fails_without_breaking_liveness() -> None:
    async def scenario() -> None:
        application = make_test_application(database_available=False)

        async with application_client(application) as client:
            readiness = await client.get("/probes/ready")
            assert readiness.status_code == 503
            assert readiness.content == b""

            liveness = await client.get("/probes/healthz")
            assert liveness.status_code == 204
            assert liveness.content == b""

    asyncio.run(scenario())


def test_readiness_fails_while_process_is_draining() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            container = application.state.dishka_container
            runtime = await container.get(RuntimeState)
            runtime.draining = True

            response = await client.get("/probes/ready")
            assert response.status_code == 503
            assert response.content == b""

    asyncio.run(scenario())


def test_readiness_fails_before_startup_completes() -> None:
    """/probes/ready alone now covers what a separate /internal/health/startup
    probe did: runtime.started is false until the lifespan sets it."""

    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            container = application.state.dishka_container
            runtime = await container.get(RuntimeState)
            runtime.started = False

            response = await client.get("/probes/ready")
            assert response.status_code == 503
            assert response.content == b""

    asyncio.run(scenario())
