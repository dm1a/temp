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
            for path, default_limit in (
                ("/api/v1/transcripts/raw", 20),
                ("/api/v1/transcripts/summarized", 50),
            ):
                response = await client.get(path, params=params, headers=MCP_AUTH_HEADERS)
                assert response.status_code == 200
                assert response.json() == {
                    "items": [],
                    "limit": default_limit,
                    "offset": 0,
                    "has_next": False,
                }

    asyncio.run(scenario())


def test_order_endpoint_validates_enum_and_pagination() -> None:
    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            valid = await client.get(
                "/api/v1/orders",
                params={"order_type": "BUY", "limit": 50, "offset": 0},
                headers=MCP_AUTH_HEADERS,
            )
            assert valid.status_code == 200
            assert valid.json() == {"items": [], "limit": 50, "offset": 0, "has_next": False}

            invalid_type = await client.get(
                "/api/v1/orders",
                params={"order_type": "HOLD"},
                headers=MCP_AUTH_HEADERS,
            )
            assert invalid_type.status_code == 422

            invalid_page = await client.get(
                "/api/v1/orders",
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
            assert valid.status_code == 200
            assert valid.json() == {"items": [], "limit": 1, "offset": 0, "has_next": False}

    asyncio.run(scenario())


def test_query_defaults_are_documented_in_openapi() -> None:
    application = make_test_application()
    schema = application.openapi()

    order_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/api/v1/orders"]["get"]["parameters"]
    }
    profile_parameters = {
        parameter["name"]: parameter
        for parameter in schema["paths"]["/api/v1/client-profile"]["get"]["parameters"]
    }

    assert order_parameters["limit"]["schema"]["default"] == 50
    assert order_parameters["offset"]["schema"]["default"] == 0
    assert "isin" not in order_parameters
    assert "instrument_name" in order_parameters["search"]["description"]
    assert profile_parameters["limit"]["schema"]["default"] == 1
    assert "CustomerProfile" in profile_parameters["search"]["description"]


def test_pagination_caps_are_enforced_on_business_endpoints() -> None:
    async def scenario() -> None:
        application = make_test_application()
        async with application_client(application) as client:
            for path, success in [
                ("/api/v1/transcripts/raw", 200),
                ("/api/v1/transcripts/summarized", 200),
                ("/api/v1/orders", 200),
                ("/api/v1/client-profile", 200),
            ]:
                params = {"advisor_phone": "advisor", "client_phone": "client"}
                valid = await client.get(
                    path,
                    params=params | {"limit": 100, "offset": 100_000},
                    headers=MCP_AUTH_HEADERS,
                )
                assert valid.status_code == success
                for invalid in [{"limit": 101}, {"offset": 100_001}, {"limit": 10**30}]:
                    response = await client.get(
                        path,
                        params=params | invalid,
                        headers=MCP_AUTH_HEADERS,
                    )
                    assert response.status_code == 422

    asyncio.run(scenario())


def test_date_ranges_require_increasing_aware_instants() -> None:
    async def scenario() -> None:
        application = make_test_application()
        async with application_client(application) as client:
            for path in [
                "/api/v1/transcripts/raw",
                "/api/v1/transcripts/summarized",
                "/api/v1/orders",
            ]:
                for dates in [
                    {"date_from": "2026-09-02T00:00:00Z", "date_to": "2026-09-01T00:00:00Z"},
                    {"date_from": "2026-09-01T03:00:00+03:00", "date_to": "2026-09-01T00:00:00Z"},
                    {"date_from": "2026-09-01T00:00:00"},
                ]:
                    response = await client.get(
                        path,
                        params={"advisor_phone": "advisor", "client_phone": "client"} | dates,
                        headers=MCP_AUTH_HEADERS,
                    )
                    assert response.status_code == 422

    asyncio.run(scenario())
