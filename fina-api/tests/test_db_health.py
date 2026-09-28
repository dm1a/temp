import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from fina.db.health import DatabaseProbe, DatabaseSchemaMismatch
from fina.db.schema import CURRENT_SCHEMA_REVISION


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


@pytest.mark.parametrize("revision", [None, "older_revision"])
def test_schema_mismatch_is_visible_in_plain_text_and_logged_again_after_recovery(caplog, revision):
    connection = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = revision
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value.__aenter__.return_value = connection
    probe = DatabaseProbe(engine)

    async def scenario():
        for _ in range(3):
            with pytest.raises(DatabaseSchemaMismatch):
                await probe.check()
        result.scalar_one_or_none.return_value = CURRENT_SCHEMA_REVISION
        await probe.check()
        result.scalar_one_or_none.return_value = revision
        with pytest.raises(DatabaseSchemaMismatch):
            await probe.check()

    asyncio.run(scenario())
    records = [record for record in caplog.records if record.name == "fina.db.health"]
    assert len(records) == 2
    for record in records:
        output = logging.Formatter("%(message)s").format(record)
        assert f"expected={CURRENT_SCHEMA_REVISION}" in output
        assert f"found={revision!r}" in output
        assert record.expected_revision == CURRENT_SCHEMA_REVISION
        assert record.found_revision == revision
