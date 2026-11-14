"""Two engines: writes go to the primary, search reads go to the (optional) replica.

In lite mode both are the same pool. In scaled mode DATABASE_READ_URL points at a
streaming replica, which is always a little behind the primary. `ReplicaGate` handles
that lag for search; `read_conn` falls back to the primary if the replica is down.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings

log = logging.getLogger(__name__)


def _engine(url: str) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_pool_overflow,
        pool_pre_ping=True,
    )


@lru_cache
def primary_engine() -> AsyncEngine:
    return _engine(get_settings().database_url)


@lru_cache
def read_engine() -> AsyncEngine:
    settings = get_settings()
    if settings.read_url == settings.database_url:
        return primary_engine()  # lite mode: share one pool
    return _engine(settings.read_url)


def has_replica() -> bool:
    return read_engine() is not primary_engine()


@asynccontextmanager
async def write_conn() -> AsyncIterator[AsyncConnection]:
    """A transaction on the primary; commits on success, rolls back on error."""
    async with primary_engine().begin() as conn:
        yield conn


@asynccontextmanager
async def _connect(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A connection from `engine`, or from the primary if `engine` (a replica) is unreachable."""
    if engine is primary_engine():
        async with engine.connect() as conn:
            yield conn
        return
    try:
        conn = await engine.connect()
    except Exception as exc:  # noqa: BLE001 - any connect failure: stay available
        log.warning("read replica unreachable, reading from the primary: %s", exc)
        conn = await primary_engine().connect()
    try:
        yield conn
    finally:
        await conn.close()


@asynccontextmanager
async def read_conn() -> AsyncIterator[AsyncConnection]:
    """Read-only work that tolerates being a moment behind (lists, statistics)."""
    async with _connect(read_engine()) as conn:
        yield conn


class ReplicaGate:
    """Keeps search from reading a replica that hasn't caught up with the latest ingest.

    Every ingest batch bumps the index version and records the primary's WAL position
    (LSN) at that moment. Search results are cached under the version, so a search must
    never compute "version N" results from data older than version N. For each new
    version the gate asks the replica once whether it has replayed up to that LSN;
    until it has, searches read from the primary. After that the answer is remembered,
    so the steady state costs nothing.
    """

    def __init__(self) -> None:
        self._caught_up_version: int | None = None

    async def engine_for(self, version: int, lsn: str | None) -> AsyncEngine:
        replica = read_engine()
        if not has_replica() or version == self._caught_up_version:
            return replica
        if lsn is not None:
            try:
                async with replica.connect() as conn:
                    caught_up = (
                        await conn.execute(
                            text("SELECT pg_last_wal_replay_lsn() >= CAST(:lsn AS pg_lsn)"),
                            {"lsn": lsn},
                        )
                    ).scalar()
            except Exception as exc:  # noqa: BLE001 - any failure: stay available
                log.warning("read replica unreachable, searching on the primary: %s", exc)
                return primary_engine()
            if not caught_up:
                return primary_engine()  # read-your-writes: the replica is still replaying
        self._caught_up_version = version
        return replica

    @asynccontextmanager
    async def connect(self, version: int, lsn: str | None) -> AsyncIterator[AsyncConnection]:
        async with _connect(await self.engine_for(version, lsn)) as conn:
            yield conn


replica_gate = ReplicaGate()


async def replication_status() -> dict[str, bool | float | None]:
    """Whether reads go to a replica, and how far behind it is (for /stats)."""
    status: dict[str, bool | float | None] = {
        "read_replica": has_replica(),
        "replica_streaming": None,
        "replica_lag_seconds": None,
    }
    if not has_replica():
        return status
    try:
        async with read_engine().connect() as conn:
            row = (
                await conn.execute(
                    text("""
                    SELECT pg_is_in_recovery() AS in_recovery,
                           CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() THEN 0
                                ELSE EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp())
                           END AS lag
                    """)
                )
            ).one()
        status["replica_streaming"] = bool(row.in_recovery)
        status["replica_lag_seconds"] = round(float(row.lag), 3) if row.lag is not None else None
    except Exception:  # noqa: BLE001 - reported as "not streaming"
        status["replica_streaming"] = False
    return status


async def dispose_engines() -> None:
    await read_engine().dispose()
    await primary_engine().dispose()
