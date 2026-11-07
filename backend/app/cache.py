"""Redis caching for search.

Two layers, both cache-aside:

1. Query embeddings: `emb:{model}:{hash(query)}`. Embedding a query is the slowest
   step of a search (a model call); the same query never needs it twice.
2. Search results: `search:v{index_version}:{hash(params)}`. The index version is
   bumped after every ingest batch, so new documents invalidate every cached
   result at once, with no per-key deletes and no stale results.

Redis is an optimisation, never a dependency: any Redis error degrades to an
uncached search instead of failing the request.
"""

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger(__name__)

INDEX_VERSION_KEY = "index:version"
HITS_KEY = "stats:search_cache:hits"
MISSES_KEY = "stats:search_cache:misses"
EMBEDDING_TTL_SECONDS = 7 * 24 * 3600


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def normalize_query(query: str) -> str:
    """Case and whitespace don't change results, so they shouldn't split the cache."""
    return " ".join(query.lower().split())


class SearchCache:
    def __init__(self, redis: Redis, search_ttl_seconds: int):
        self.redis = redis
        self.search_ttl = search_ttl_seconds

    async def _safe(self, op: Awaitable[Any], default: Any = None) -> Any:
        try:
            return await op
        except Exception as exc:  # noqa: BLE001 - cache must never break search
            log.warning("redis unavailable, continuing without cache: %s", exc)
            return default

    # ── index version ────────────────────────────────────────────────────
    async def index_version(self, load_from_db: Callable[[], Awaitable[int]]) -> int:
        cached = await self._safe(self.redis.get(INDEX_VERSION_KEY))
        if cached is not None:
            return int(cached)
        version = await load_from_db()
        await self._safe(self.redis.set(INDEX_VERSION_KEY, version))
        return version

    # ── query embeddings ─────────────────────────────────────────────────
    def _embedding_key(self, model: str, query: str) -> str:
        return f"emb:{model}:{_digest(normalize_query(query))}"

    async def get_embedding(self, model: str, query: str) -> str | None:
        value = await self._safe(self.redis.get(self._embedding_key(model, query)))
        return value.decode() if isinstance(value, bytes) else value

    async def set_embedding(self, model: str, query: str, vector_literal: str) -> None:
        await self._safe(
            self.redis.set(
                self._embedding_key(model, query), vector_literal, ex=EMBEDDING_TTL_SECONDS
            )
        )

    # ── search results ───────────────────────────────────────────────────
    def search_key(self, version: int, params: dict[str, Any]) -> str:
        params = {**params, "q": normalize_query(params["q"])}
        return f"search:v{version}:{_digest(params)}"

    async def get_search(self, key: str) -> list[dict] | None:
        raw = await self._safe(self.redis.get(key))
        await self._safe(self.redis.incr(HITS_KEY if raw is not None else MISSES_KEY))
        return json.loads(raw) if raw is not None else None

    async def set_search(self, key: str, results: list[dict]) -> None:
        await self._safe(self.redis.set(key, json.dumps(results), ex=self.search_ttl))

    # ── generic JSON entries (Ask mode answers) ──────────────────────────
    def key(self, prefix: str, version: int, params: dict[str, Any]) -> str:
        return f"{prefix}:v{version}:{_digest(params)}"

    async def get_json(self, key: str) -> Any:
        raw = await self._safe(self.redis.get(key))
        return json.loads(raw) if raw is not None else None

    async def set_json(self, key: str, value: Any) -> None:
        await self._safe(self.redis.set(key, json.dumps(value), ex=self.search_ttl))

    async def hit_rate(self) -> dict[str, float | int]:
        hits, misses = await self._safe(self.redis.mget(HITS_KEY, MISSES_KEY), [None, None])
        hits, misses = int(hits or 0), int(misses or 0)
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 3) if total else 0.0,
        }


async def publish_index_version(redis: Redis, version: int) -> None:
    """Called by the worker after a batch; instantly retires all cached results."""
    try:
        await redis.set(INDEX_VERSION_KEY, version)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not publish index version to redis: %s", exc)
