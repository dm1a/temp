import asyncio

from tests.conftest import MCP_AUTH_HEADERS, application_client, make_test_application


def test_transcript_endpoints_validate_and_reach_stub() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            missing = await client.get(
                "/api/v1/transcripts/raw",
                headers=MCP_AUTH_HEADERS,
            )
            assert missing.status_code == 422

            params = {
                "advisor_phone": "79990000001",
                "client_phone": "79990000002",
                "date_from": "2026-09-01T00:00:00Z",
                "date_to": "2026-09-02T00:00:00Z",
                "search": "облигации",
            }
            for path in (
                "/api/v1/transcripts/raw",
                "/api/v1/transcripts/summarized",
            ):
                response = await client.get(path, params=params, headers=MCP_AUTH_HEADERS)
                assert response.status_code == 200
                assert response.json() == {
                    "items": [],
                    "limit": 50,
                    "offset": 0,
                    "has_next": False,
                }

    asyncio.run(scenario())


def test_deal_endpoint_validates_enum_and_pagination() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            valid = await client.get(
                "/api/v1/deals",
                params={"deal_type": "BUY", "limit": 50, "offset": 0},
                headers=MCP_AUTH_HEADERS,
            )
            assert valid.status_code == 501

            invalid_type = await client.get(
                "/api/v1/deals",
                params={"deal_type": "HOLD"},
                headers=MCP_AUTH_HEADERS,
            )
            assert invalid_type.status_code == 422

            invalid_page = await client.get(
                "/api/v1/deals",
                params={"limit": 0, "offset": -1},
                headers=MCP_AUTH_HEADERS,
            )
            assert invalid_page.status_code == 422

    asyncio.run(scenario())


def test_client_profile_requires_client_phone() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            missing = await client.get(
                "/api/v1/client-profile",
                headers=MCP_AUTH_HEADERS,
            )
            assert missing.status_code == 422

            valid = await client.get(
                "/api/v1/client-profile",
                params={"client_phone": "79990000002"},
                headers=MCP_AUTH_HEADERS,
            )
            assert valid.status_code == 501

    asyncio.run(scenario())


def test_query_defaults_are_documented_in_openapi() -> None:
    application = make_test_application()
    schema = application.openapi()

    deal_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/api/v1/deals"]["get"]["parameters"]
    }
    profile_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/api/v1/client-profile"]["get"]["parameters"]
    }

    assert deal_parameters["limit"]["schema"]["default"] == 50
    assert deal_parameters["offset"]["schema"]["default"] == 0
    assert "isin" not in deal_parameters
    assert "instrument_name" in deal_parameters["search"]["description"]
    assert profile_parameters["limit"]["schema"]["default"] == 1
    assert "CustomerProfile" in profile_parameters["search"]["description"]
