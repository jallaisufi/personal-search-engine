import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.ask import SYSTEM_PROMPT, build_user_prompt, cited_numbers
from app.cache import SearchCache
from app.config import get_settings
from app.db import read_conn
from app.ingest.chunk import stitch_text
from app.ingest.embed import to_pgvector
from app.llm import LLMError, LLMProvider
from app.search.hybrid import hybrid_search

log = logging.getLogger(__name__)
router = APIRouter()


class SearchResult(BaseModel):
    chunk_id: int
    document_id: int
    title: str
    path: str
    web_url: str | None
    page: int | None
    heading_path: str | None
    char_start: int
    char_end: int
    doc_length: int
    snippet_html: str
    score: float
    matched_by: list[str]


class SearchResponse(BaseModel):
    query: str
    took_ms: float
    cached: bool
    results: list[SearchResult]


class DocumentOut(BaseModel):
    id: int
    title: str
    path: str
    status: str
    chunks: int
    modified_at: datetime | None
    indexed_at: datetime | None
    error: str | None


class ChunkLocation(BaseModel):
    id: int
    char_start: int
    char_end: int
    page: int | None
    heading_path: str | None


class DocumentDetail(BaseModel):
    id: int
    title: str
    path: str
    web_url: str | None
    mime_type: str | None
    status: str
    modified_at: datetime | None
    text: str
    chunks: list[ChunkLocation]


class CacheStats(BaseModel):
    hits: int
    misses: int
    hit_rate: float


class Stats(BaseModel):
    documents: dict[str, int]
    chunks: int
    index_version: int
    embedding_model: str | None
    search_cache: CacheStats


@router.get("/")
async def root() -> dict:
    return {
        "name": "personal-search",
        "version": "0.1.0",
        "endpoints": ["/search?q=...", "POST /ask", "/documents", "/stats", "/health", "/docs"],
    }


@router.get("/health")
async def health(request: Request) -> dict:
    checks: dict[str, str] = {}
    try:
        async with read_conn() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"error: {type(exc).__name__}"
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"error: {type(exc).__name__}"

    if any(v != "ok" for v in checks.values()):
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ok", **checks}


async def load_index_version() -> int:
    async with read_conn() as conn:
        result = await conn.execute(text("SELECT version FROM index_state WHERE id = 1"))
        return int(result.scalar_one())


async def run_search(request: Request, query: str, limit: int) -> tuple[list[dict], bool]:
    """Cached hybrid search. Returns (results, served_from_cache)."""
    settings = get_settings()
    cache: SearchCache = request.app.state.cache
    embedder = request.app.state.embedder

    # 1. Whole result cached for this index version?
    version = await cache.index_version(load_index_version)
    key = cache.search_key(
        version,
        {"q": query, "limit": limit, "k": settings.rrf_k, "n": settings.search_candidates},
    )
    if (cached := await cache.get_search(key)) is not None:
        return cached, True

    # 2. Query embedding cached? (the slowest step: a model call)
    vector = await cache.get_embedding(embedder.model, query)
    if vector is None:
        vector = to_pgvector(await embedder.embed_query(query))
        await cache.set_embedding(embedder.model, query, vector)

    # 3. Run the hybrid search on the read connection.
    async with read_conn() as conn:
        hits = await hybrid_search(
            conn,
            query,
            vector,
            limit=limit,
            candidates=settings.search_candidates,
            rrf_k=settings.rrf_k,
        )
    results = [asdict(h) for h in hits]
    await cache.set_search(key, results)
    return results, False


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    response: Response,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=50),
) -> SearchResponse:
    started = time.perf_counter()
    query = " ".join(q.split())
    results, cached = await run_search(request, query, limit)
    response.headers["X-Cache"] = "HIT" if cached else "MISS"
    return SearchResponse(
        query=query,
        took_ms=round((time.perf_counter() - started) * 1000, 1),
        cached=cached,
        results=[SearchResult(**r) for r in results],
    )


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def chunk_texts(chunk_ids: list[int]) -> dict[int, str]:
    if not chunk_ids:
        return {}
    async with read_conn() as conn:
        rows = await conn.execute(
            text("SELECT id, text FROM chunks WHERE id = ANY(:ids)"), {"ids": chunk_ids}
        )
        return {r.id: r.text for r in rows}


@router.post("/ask")
async def ask(request: Request, body: AskRequest) -> StreamingResponse:
    """Answer a question from your documents, streamed as server-sent events.

    Events, in order: `sources` (the numbered passages), `token` (pieces of the answer
    as the model writes them), then `done` (citations used, timing) or `error`.
    """
    settings = get_settings()
    cache: SearchCache = request.app.state.cache
    llm: LLMProvider = request.app.state.llm
    question = " ".join(body.question.split())

    async def events() -> AsyncIterator[str]:
        started = time.perf_counter()

        def elapsed() -> float:
            return round((time.perf_counter() - started) * 1000, 1)

        version = await cache.index_version(load_index_version)
        answer_key = cache.key(
            "ask",
            version,
            {"q": question.lower(), "model": llm.model, "n": settings.ask_sources},
        )
        if (hit := await cache.get_json(answer_key)) is not None:
            yield sse("sources", hit["sources"])
            yield sse("token", {"text": hit["answer"]})
            yield sse("done", {"cited": hit["cited"], "took_ms": elapsed(), "cached": True})
            return

        results, _ = await run_search(request, question, settings.ask_sources)
        texts = await chunk_texts([r["chunk_id"] for r in results])
        sources = [{**r, "n": i} for i, r in enumerate(results, start=1)]
        yield sse("sources", sources)

        if not sources:
            yield sse("token", {"text": "I couldn't find this in your documents."})
            yield sse("done", {"cited": [], "took_ms": elapsed(), "cached": False})
            return

        prompt_sources = [{**s, "text": texts.get(s["chunk_id"], "")} for s in sources]
        answer = ""
        try:
            async for piece in llm.stream(
                SYSTEM_PROMPT, build_user_prompt(question, prompt_sources)
            ):
                answer += piece
                yield sse("token", {"text": piece})
        except LLMError as exc:
            yield sse("error", {"message": str(exc)})
            return
        except Exception:
            log.exception("ask failed")
            yield sse("error", {"message": "The model failed while answering. Try again."})
            return

        cited = cited_numbers(answer, len(sources))
        yield sse("done", {"cited": cited, "took_ms": elapsed(), "cached": False})
        await cache.set_json(answer_key, {"answer": answer, "sources": sources, "cited": cited})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/documents", response_model=list[DocumentOut])
async def documents(
    status: Literal["pending", "indexed", "failed"] | None = None,
) -> list[DocumentOut]:
    async with read_conn() as conn:
        rows = await conn.execute(
            text("""
            SELECT d.id, d.title, d.external_id AS path, d.status, d.modified_at,
                   d.indexed_at, d.error, count(c.id) AS chunks
            FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
            WHERE CAST(:status AS text) IS NULL OR d.status = :status
            GROUP BY d.id
            ORDER BY d.title
            """),
            {"status": status},
        )
        return [DocumentOut(**r._mapping) for r in rows]


@router.get("/documents/{document_id}", response_model=DocumentDetail)
async def document_detail(document_id: int) -> DocumentDetail:
    async with read_conn() as conn:
        doc = (
            await conn.execute(
                text("""
                SELECT id, title, external_id AS path, web_url, mime_type, status, modified_at
                FROM documents WHERE id = :id
                """),
                {"id": document_id},
            )
        ).one_or_none()
        if doc is None:
            raise HTTPException(status_code=404, detail="Document not found")
        rows = (
            await conn.execute(
                text("""
                SELECT id, char_start, char_end, page, heading_path, text
                FROM chunks WHERE document_id = :id ORDER BY char_start
                """),
                {"id": document_id},
            )
        ).all()

    return DocumentDetail(
        **doc._mapping,
        text=stitch_text([(r.char_start, r.char_end, r.text) for r in rows]),
        chunks=[
            ChunkLocation(
                id=r.id,
                char_start=r.char_start,
                char_end=r.char_end,
                page=r.page,
                heading_path=r.heading_path,
            )
            for r in rows
        ],
    )


@router.get("/stats", response_model=Stats)
async def stats(request: Request) -> Stats:
    async with read_conn() as conn:
        by_status = await conn.execute(
            text("SELECT status, count(*) FROM documents GROUP BY status")
        )
        documents = {row[0]: row[1] for row in by_status}
        state = (
            await conn.execute(
                text(
                    "SELECT version, embedding_model, (SELECT count(*) FROM chunks) AS chunks "
                    "FROM index_state WHERE id = 1"
                )
            )
        ).one()
    return Stats(
        documents=documents,
        chunks=state.chunks,
        index_version=state.version,
        embedding_model=state.embedding_model,
        search_cache=CacheStats(**await request.app.state.cache.hit_rate()),
    )
