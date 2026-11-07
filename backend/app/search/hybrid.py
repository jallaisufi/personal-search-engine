"""Hybrid search: vector similarity + full-text, merged with Reciprocal Rank Fusion.

Why both: embeddings understand meaning ("rent increase" ≈ "raising the rent") but
are weak at exact tokens (names, invoice numbers, acronyms); full-text is the reverse.
RRF merges the two rankings without having to calibrate their very different scores:

    score(chunk) = Σ over retrievers  1 / (k + rank)
"""

import html
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# Control characters as highlight markers: the snippet is HTML-escaped first, then the
# markers become <mark> tags, so document text can never inject HTML into the UI.
_START, _STOP = "\x02", "\x03"
_HEADLINE_OPTS = f'StartSel="{_START}", StopSel="{_STOP}", MaxWords=40, MinWords=15, MaxFragments=2'

HYBRID_SQL = text("""
WITH q AS (
    SELECT websearch_to_tsquery('simple', :query) AS tsq
),
vec AS (
    SELECT id, row_number() OVER (ORDER BY dist) AS rank
    FROM (
        SELECT id, embedding <=> CAST(CAST(:qvec AS text) AS vector) AS dist
        FROM chunks
        ORDER BY dist
        LIMIT :candidates
    ) v
),
kw AS (
    SELECT id, row_number() OVER (ORDER BY r DESC) AS rank
    FROM (
        SELECT c.id, ts_rank_cd(c.tsv, q.tsq) AS r
        FROM chunks c, q
        WHERE c.tsv @@ q.tsq
        ORDER BY r DESC
        LIMIT :candidates
    ) k
),
fused AS (
    SELECT COALESCE(vec.id, kw.id) AS id,
           COALESCE(1.0 / (:rrf_k + vec.rank), 0)
         + COALESCE(1.0 / (:rrf_k + kw.rank), 0) AS score,
           vec.rank AS vec_rank,
           kw.rank AS kw_rank
    FROM vec FULL OUTER JOIN kw ON vec.id = kw.id
    ORDER BY score DESC
    LIMIT :limit
)
SELECT f.id AS chunk_id, f.score, f.vec_rank, f.kw_rank,
       d.id AS document_id, d.title, d.external_id, d.web_url, d.mime_type,
       c.page, c.heading_path, c.char_start, c.char_end,
       (SELECT max(c2.char_end) FROM chunks c2 WHERE c2.document_id = d.id) AS doc_length,
       ts_headline('simple', c.text, q.tsq, :headline_opts) AS snippet
FROM fused f
JOIN chunks c ON c.id = f.id
JOIN documents d ON d.id = c.document_id
CROSS JOIN q
ORDER BY f.score DESC
""")


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    document_id: int
    title: str
    path: str
    web_url: str | None
    page: int | None
    heading_path: str | None
    char_start: int
    char_end: int
    doc_length: int  # lets the UI show where in the document the passage sits
    snippet_html: str
    score: float
    matched_by: list[str]


def _safe_snippet(raw: str) -> str:
    return html.escape(raw).replace(_START, "<mark>").replace(_STOP, "</mark>")


async def hybrid_search(
    conn: AsyncConnection,
    query: str,
    query_vector: str,
    *,
    limit: int = 10,
    candidates: int = 50,
    rrf_k: int = 60,
) -> list[SearchHit]:
    # HNSW returns at most ef_search rows; the default (40) is below our candidate count.
    await conn.execute(
        text("SELECT set_config('hnsw.ef_search', :ef, true)"),
        {"ef": str(max(100, candidates * 2))},
    )
    rows = await conn.execute(
        HYBRID_SQL,
        {
            "query": query,
            "qvec": query_vector,
            "candidates": candidates,
            "rrf_k": rrf_k,
            "limit": limit,
            "headline_opts": _HEADLINE_OPTS,
        },
    )
    hits = []
    for r in rows:
        matched_by = [
            name for name, rank in (("semantic", r.vec_rank), ("keyword", r.kw_rank)) if rank
        ]
        hits.append(
            SearchHit(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                title=r.title,
                path=r.external_id,
                web_url=r.web_url,
                page=r.page,
                heading_path=r.heading_path,
                char_start=r.char_start,
                char_end=r.char_end,
                doc_length=r.doc_length,
                snippet_html=_safe_snippet(r.snippet),
                score=float(r.score),
                matched_by=matched_by,
            )
        )
    return hits
