# ADR 002: HNSW index and hybrid retrieval with Reciprocal Rank Fusion

**Status:** accepted · 2026-11

## Context

Pure vector search understands meaning but misses exact tokens: names, invoice numbers, acronyms like "mTLS". Pure keyword search finds exact tokens but misses paraphrases. The two produce scores on unrelated scales (cosine distance vs. `ts_rank_cd`), so they can't simply be added.

## Decision

1. **Vector index: HNSW** (`m = 16`, `ef_construction = 64`, cosine distance). Unlike IVFFlat it needs no training step on existing data and handles a continuously changing index well. At query time `hnsw.ef_search` is raised above the candidate count, because HNSW returns at most `ef_search` rows (default 40).
2. **Keyword index: GIN on a generated `tsvector`** using the `simple` configuration, which works for mixed English/German documents (no language-specific stemming).
3. **Fusion: RRF**, `score = Σ 1 / (k + rank)` with `k = 60`, over the top 50 candidates from each retriever. RRF uses only ranks, so no score calibration is needed.

## Consequences

- Queries like "mTLS" and "raising the rent" both work.
- Each result records whether it matched semantically, by keyword, or both, which is useful for debugging and for the planned search-quality eval.
- `simple` means "payments" doesn't match "payment" in the keyword leg; the vector leg covers this. Per-language configurations are a possible later improvement.
