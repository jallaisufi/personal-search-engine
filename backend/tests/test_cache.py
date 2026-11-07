from app.cache import INDEX_VERSION_KEY, SearchCache, normalize_query, publish_index_version
from tests.fakes import BrokenRedis, InMemoryRedis


async def load_seven() -> int:
    return 7


def test_normalize_query_ignores_case_and_spacing():
    assert normalize_query("  Security   DEPOSIT ") == "security deposit"


async def test_search_key_changes_with_index_version():
    cache = SearchCache(InMemoryRedis(), 600)
    params = {"q": "security deposit", "limit": 10}
    assert cache.search_key(1, params) != cache.search_key(2, params)
    assert cache.search_key(1, params) == cache.search_key(1, {**params, "q": "Security  Deposit"})


async def test_search_results_round_trip_and_hit_rate():
    redis = InMemoryRedis()
    cache = SearchCache(redis, search_ttl_seconds=600)
    key = cache.search_key(3, {"q": "rent"})

    assert await cache.get_search(key) is None  # miss
    await cache.set_search(key, [{"chunk_id": 1}])
    assert await cache.get_search(key) == [{"chunk_id": 1}]  # hit
    assert redis.ttls[key] == 600
    assert await cache.hit_rate() == {"hits": 1, "misses": 1, "hit_rate": 0.5}


async def test_embeddings_are_cached_per_model():
    cache = SearchCache(InMemoryRedis(), 600)
    await cache.set_embedding("model-a", "Rent", "[1,0]")
    assert await cache.get_embedding("model-a", "rent") == "[1,0]"
    assert await cache.get_embedding("model-b", "rent") is None


async def test_index_version_falls_back_to_database_and_is_then_cached():
    redis = InMemoryRedis()
    cache = SearchCache(redis, 600)
    assert await cache.index_version(load_seven) == 7
    assert redis.data[INDEX_VERSION_KEY] == b"7"

    await publish_index_version(redis, 8)  # what the worker does after a sync
    assert await cache.index_version(load_seven) == 8


async def test_redis_outage_degrades_to_uncached():
    cache = SearchCache(BrokenRedis(), 600)  # type: ignore[arg-type]
    assert await cache.index_version(load_seven) == 7
    assert await cache.get_search("any") is None
    await cache.set_search("any", [])
    assert await cache.get_embedding("m", "q") is None
    assert (await cache.hit_rate())["hits"] == 0
    await publish_index_version(BrokenRedis(), 1)  # type: ignore[arg-type]
