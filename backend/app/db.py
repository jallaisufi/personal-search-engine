"""Two engines: writes go to the primary, search reads go to the (optional) replica."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings


@lru_cache
def primary_engine() -> AsyncEngine:
    return create_async_engine(
        get_settings().database_url, pool_size=10, max_overflow=5, pool_pre_ping=True
    )


@lru_cache
def read_engine() -> AsyncEngine:
    settings = get_settings()
    if settings.read_url == settings.database_url:
        return primary_engine()  # lite mode: share one pool
    return create_async_engine(settings.read_url, pool_size=10, max_overflow=5, pool_pre_ping=True)


@asynccontextmanager
async def write_conn() -> AsyncIterator[AsyncConnection]:
    """A transaction on the primary; commits on success, rolls back on error."""
    async with primary_engine().begin() as conn:
        yield conn


@asynccontextmanager
async def read_conn() -> AsyncIterator[AsyncConnection]:
    async with read_engine().connect() as conn:
        yield conn


async def dispose_engines() -> None:
    await read_engine().dispose()
    await primary_engine().dispose()
