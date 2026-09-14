import asyncio

from tests.conftest import application_client, make_test_application


def _counts_by_route(body: str) -> dict[str, float]:
    """Parses fina_http_requests_total lines into {route_label: value}.

    Sums across method/status combinations for a given route, since this
    test only cares about the route dimension.
    """
    counts: dict[str, float] = {}
    for line in body.splitlines():
        if not line.startswith("fina_http_requests_total{"):
            continue
        route = line.split('route="')[1].split('"')[0]
        value = float(line.rsplit(" ", 1)[1])
        counts[route] = counts.get(route, 0.0) + value
    return counts


def test_metrics_are_recorded_with_bounded_route_labels() -> None:
    """Request count and duration must be labeled by the matched route
    template, not the raw request path -- so an arbitrary or malformed
    path can't create unbounded label cardinality. Every unmatched request
    collapses into one "unmatched" label, no matter how many distinct
    garbage paths were requested.

    The Prometheus registry is process-global (by design -- one process,
    one /metrics endpoint, aggregating everything), and the same
    application-building test fixture is reused across the whole suite, so
    this compares before/after deltas rather than asserting absolute
    counts.
    """

    async def scenario() -> None:
        application = make_test_application()

        async with application_client(application) as client:
            before = _counts_by_route((await client.get("/metrics")).text)

            await client.get("/probes/healthz")
            await client.get("/probes/healthz")
            await client.get("/garbage-path-1")
            await client.get("/garbage-path-2")
            await client.get("/garbage-path-3")

            response = await client.get("/metrics")
            assert response.status_code == 200
            after = _counts_by_route(response.text)

        delta = {
            route: after.get(route, 0.0) - before.get(route, 0.0)
            for route in after.keys() | before.keys()
        }

        assert delta.get("/probes/healthz") == 2.0
        # Three distinct garbage paths still collapse into one label,
        # incrementing by 3 -- not three separate labels each by 1.
        assert delta.get("unmatched") == 3.0

    asyncio.run(scenario())
