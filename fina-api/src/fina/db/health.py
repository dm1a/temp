import asyncio
import logging
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fina.db.schema import CURRENT_SCHEMA_REVISION

logger = logging.getLogger(__name__)

# A readiness probe must answer fast: Kubernetes' own probe timeout is
# typically a few seconds, and a slow "ready" response is as disruptive to
# a rolling deploy as a wrong one. This is deliberately much shorter than
# the provider-operation deadline workers use (see db/session.py) -- a
# worker can afford to wait longer for a slow statement than a readiness
# probe can afford to wait for an answer.
DEFAULT_CHECK_TIMEOUT = timedelta(seconds=2)


class DatabaseSchemaMismatch(RuntimeError):
    """Readiness failed because the database is on a different schema revision."""


class DatabaseProbe:
    """Checks both PostgreSQL connectivity and the applied Alembic revision."""

    def __init__(self, engine: AsyncEngine, *, timeout: timedelta = DEFAULT_CHECK_TIMEOUT) -> None:
        self._engine = engine
        self._timeout = timeout
        self._last_revision: str | None = CURRENT_SCHEMA_REVISION

    async def check(self) -> None:
        async with asyncio.timeout(self._timeout.total_seconds()):
            async with self._engine.connect() as connection:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                revision = result.scalar_one_or_none()

                if revision != CURRENT_SCHEMA_REVISION:
                    # The readiness probe only reports the exception type
                    # (probes.py keeps driver text out of logs), so the two
                    # revisions are recorded here -- they are the whole
                    # diagnosis when a migration Job has not run yet, or has
                    # run ahead of this replica's image. See k8s/README.md.
                    if revision != self._last_revision:
                        logger.error(
                            "database schema revision is not the one this image expects",
                            extra={
                                "expected_revision": CURRENT_SCHEMA_REVISION,
                                "found_revision": revision,
                            },
                        )
                    self._last_revision = revision
                    raise DatabaseSchemaMismatch("database schema revision is not current")
                self._last_revision = revision
