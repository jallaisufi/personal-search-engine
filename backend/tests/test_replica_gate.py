"""Replica lag handling. CI has one Postgres, so the "replica" here is a second engine:
pointed at the primary (which never reports a replay position, i.e. looks like a replica
that hasn't caught up) or at a closed port (a replica that is down). The real
primary → replica → API path is exercised by scripts/smoke_scaled.sh.
"""

import re

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.db as db
from app.config import get_settings
from app.sync import publish_changes
from tests.fakes import InMemoryRedis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def lagging_replica(monkeypatch):
    engine = create_async_engine(get_settings().database_url)
    monkeypatch.setattr(db, "read_engine", lambda: engine)
    yield engine
    await engine.dispose()


@pytest.fixture
async def dead_replica(monkeypatch):
    engine = create_async_engine("postgresql+asyncpg://app:app@127.0.0.1:1/search")
    monkeypatch.setattr(db, "read_engine", lambda: engine)
    yield engine
    await engine.dispose()


async def test_without_a_replica_everything_uses_the_primary():
    gate = db.ReplicaGate()
    assert db.has_replica() is False
    assert await gate.engine_for(1, "0/0") is db.primary_engine()
    assert (await db.replication_status())["read_replica"] is False


async def test_publishing_changes_records_the_wal_position():
    redis = InMemoryRedis()
    version = await publish_changes(redis)
    assert redis.data["index:version"] == str(version).encode()
    assert re.fullmatch(rb"[0-9A-F]+/[0-9A-F]+", redis.data["index:lsn"])


async def test_search_reads_the_primary_until_the_replica_has_caught_up(lagging_replica):
    gate = db.ReplicaGate()
    assert db.has_replica() is True
    assert await gate.engine_for(7, "0/1") is db.primary_engine()  # not replayed yet
    assert await gate.engine_for(7, "0/1") is db.primary_engine()  # asked again, same answer


async def test_a_caught_up_replica_is_used_and_remembered(lagging_replica, monkeypatch):
    gate = db.ReplicaGate()
    assert await gate.engine_for(7, None) is lagging_replica  # nothing to wait for
    # Remembered for this version: no further check, even with a position it can't have.
    assert await gate.engine_for(7, "FFFFFFFF/FFFFFFFF") is lagging_replica
    assert await gate.engine_for(8, "FFFFFFFF/FFFFFFFF") is db.primary_engine()  # new version


async def test_an_unreachable_replica_falls_back_to_the_primary(dead_replica):
    gate = db.ReplicaGate()
    assert await gate.engine_for(3, "0/1") is db.primary_engine()

    async with db.read_conn() as conn:  # ordinary reads fall back too
        assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    async with gate.connect(3, None) as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar() == 1

    status = await db.replication_status()
    assert status == {"read_replica": True, "replica_streaming": False, "replica_lag_seconds": None}
