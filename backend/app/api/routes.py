import time
from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text

from app.config import get_settings
from app.db import read_conn
from app.ingest.chunk import stitch_text
from app.ingest.embed import to_pgvector
from app.search.hybrid import hybrid_search

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


class Stats(BaseModel):
    documents: dict[str, int]
    chunks: int
    index_version: int
    embedding_model: str | None


@router.get("/")
async def root() -> dict:
    return {
        "name": "personal-search",
        "version": "0.1.0",
        "endpoints": ["/search?q=...", "/documents", "/stats", "/health", "/docs"],
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


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=50),
) -> SearchResponse:
    started = time.perf_counter()
    settings = get_settings()
    query = " ".join(q.split())

    vector = await request.app.state.embedder.embed_query(query)
    async with read_conn() as conn:
        hits = await hybrid_search(
            conn,
            query,
            to_pgvector(vector),
            limit=limit,
            candidates=settings.search_candidates,
            rrf_k=settings.rrf_k,
        )

    return SearchResponse(
        query=query,
        took_ms=round((time.perf_counter() - started) * 1000, 1),
        results=[SearchResult(**asdict(h)) for h in hits],
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
async def stats() -> Stats:
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
    )
