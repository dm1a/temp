import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from prometheus_client import Counter, Histogram

logger = logging.getLogger("fina.http")

HTTP_REQUESTS_TOTAL = Counter(
    "fina_http_requests_total",
    "Total HTTP requests, labeled by method, matched route template, and status code.",
    ["method", "route", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "fina_http_request_duration_seconds",
    "HTTP request duration in seconds, labeled by method and matched route template.",
    ["method", "route"],
)

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_UNMATCHED_ROUTE_LABEL = "unmatched"


class MetricsMiddleware:
    """Records metrics and a safe request summary for HTTP requests.

    Labeled by method and the matched route *template* (e.g.
    "/api/v1/orders"), never the raw request path -- so label cardinality
    stays bounded regardless of query parameters or arbitrary/garbage
    paths. A request that matches no route (a 404 before routing
    completes) is labeled "unmatched" rather than the requested path, for
    the same reason: an attacker or a typo probing random paths must not
    be able to create unbounded label cardinality.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        status_code = 500
        start = time.perf_counter()
        cancelled = False

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            # Recent FastAPI versions keep include_router prefixes on the
            # effective route; scope["route"] can be the unprefixed original.
            route = scope.get("fastapi", {}).get("effective_route_context") or scope.get("route")
            route_label = route.path if route is not None else _UNMATCHED_ROUTE_LABEL
            duration = time.perf_counter() - start
            HTTP_REQUESTS_TOTAL.labels(
                method=method, route=route_label, status=str(status_code)
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(method=method, route=route_label).observe(duration)
            # Readiness failures/recovery are logged as transitions by probes.py.
            # Routine health checks and metrics scrapes should stay quiet.
            # Never `return` from this finally block: that would swallow the
            # CancelledError (or any other exception) on its way out.
            routine_probe = route_label.startswith("/probes/") or (
                route_label == "/metrics" and status_code < 400
            )
            if not routine_probe:
                level = (
                    logging.WARNING
                    if cancelled
                    else logging.ERROR
                    if status_code >= 500
                    else logging.WARNING
                    if status_code >= 400
                    else logging.INFO
                )
                logger.log(
                    level,
                    "HTTP request cancelled" if cancelled else "HTTP request finished",
                    extra={
                        "method": method,
                        "route": route_label,
                        "status": status_code,
                        "duration_ms": round(duration * 1000, 2),
                    },
                )
