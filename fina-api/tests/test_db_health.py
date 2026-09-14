import asyncio
from datetime import timedelta

import pytest

from fina.db.health import DatabaseProbe


class _HangingConnection:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def __aenter__(self) -> "_HangingConnection":
        await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("must not run a query once the connection itself has hung")


class _HangingEngine:
    def __init__(self, delay: float) -> None:
        self._delay = delay

    def connect(self) -> _HangingConnection:
        return _HangingConnection(self._delay)


def test_check_raises_instead_of_hanging_past_its_timeout() -> None:
    """A slow/hung database must not hang the caller (a readiness probe, in
    practice) indefinitely -- check() must give up on its own, well within
    a caller-imposed outer bound."""

    async def scenario() -> None:
        probe = DatabaseProbe(_HangingEngine(delay=10), timeout=timedelta(seconds=0.05))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(probe.check(), timeout=2)

    asyncio.run(scenario())
