import asyncio

from tests.conftest import MCP_AUTH_HEADERS, application_client, make_test_application


def test_business_endpoints_require_valid_bearer_key() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            missing = await client.get(
                "/api/v1/transcripts/raw",
                params={"advisor_phone": "79990000001", "client_phone": "79990000002"},
            )
            assert missing.status_code == 401
            assert missing.json() == {"detail": "Invalid authentication credentials"}
            assert missing.headers["www-authenticate"] == "Bearer"

            incorrect = await client.get(
                "/api/v1/transcripts/raw",
                params={"advisor_phone": "79990000001", "client_phone": "79990000002"},
                headers={"Authorization": "Bearer wrong-key"},
            )
            assert incorrect.status_code == 401
            assert incorrect.json() == {"detail": "Invalid authentication credentials"}

            correct = await client.get(
                "/api/v1/transcripts/raw",
                params={"advisor_phone": "79990000001", "client_phone": "79990000002"},
                headers=MCP_AUTH_HEADERS,
            )
            assert correct.status_code == 200

    asyncio.run(scenario())


def test_health_endpoints_remain_public() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            response = await client.get("/internal/health/live")
            assert response.status_code == 204

    asyncio.run(scenario())
