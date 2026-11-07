# ADR 003: Versioned cache keys for search results

**Status:** accepted · 2026-11

## Context

Repeated searches (the same question asked again, a page reload, several people on one instance) redo the slowest work: embedding the query with a model and running two index scans. Caching helps, but a cached result must never hide a document that was added after it was cached.

## Decision

Cache-aside in Redis, in two layers:

1. **Query embeddings**, keyed by model and normalised query, kept for 7 days. They never go stale: the same model always returns the same vector for the same text.
2. **Search results**, keyed by `search:v{index_version}:{hash(query, limit, ranking params)}`, kept for 10 minutes.

`index_state.version` is bumped in Postgres after every sync that changed something, and the worker publishes it to Redis. API replicas read the version from Redis (falling back to Postgres) and build every key with it. A new version makes every older entry unreachable at once; they expire on their own TTL.

Queries are normalised (case, whitespace) before hashing, so trivial variations share an entry.

## Consequences

- No per-key invalidation logic and no scans over Redis keys; correctness depends on one integer.
- Any ingest invalidates the whole result cache, even for unrelated queries. For a personal index that changes a few times a day this costs little; a high-churn index would want finer-grained versions.
- Redis is optional at runtime: every cache call is wrapped so that an outage degrades to uncached search.
- Known gap for the scaled setup: if the worker publishes a new version before the read replica has replayed the new rows, a search could cache results from the replica's older state under the new version for up to the TTL. Weekend 4 handles this together with replication lag (e.g. publish the version only once the replica has caught up).
