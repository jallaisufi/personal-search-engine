# ADR 001: Postgres + pgvector instead of a dedicated vector database

**Status:** accepted · 2026-11

## Context

Search needs vector similarity, full-text search, and relational metadata (documents, sources, deadlines). Options were a dedicated vector database (Qdrant, Weaviate, Milvus) next to a relational database, or Postgres with the pgvector extension.

## Decision

Use Postgres 16 with pgvector for everything.

## Consequences

- **One query does hybrid search.** Vector ranking, full-text ranking and the RRF merge run in a single SQL statement, joined directly to document metadata. With two stores, the API would fan out, merge in application code, and keep IDs in sync.
- **One consistency model.** Re-indexing a document replaces its chunks in one transaction, so search never sees a half-updated document.
- **Replicas come for free.** Postgres streaming replication gives read replicas for search without a second replication setup.
- **Fewer moving parts** for self-hosters: one database to back up and upgrade.
- **Trade-off:** dedicated vector databases scale further (hundreds of millions of vectors, sharding, quantization). A personal document collection is in the thousands to low millions of chunks, well within pgvector's range. If that changes, the search module is the only code that would move.
