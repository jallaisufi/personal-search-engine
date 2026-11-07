"""Index one document: extract → chunk → embed (reusing known vectors) → write.

Embedding happens before the write transaction so the transaction stays short.
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.config import Settings
from app.db import primary_engine, write_conn
from app.ingest.chunk import Chunk, chunk_blocks
from app.ingest.embed import EmbeddingProvider, to_pgvector
from app.ingest.extract import extract

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentInput:
    source_id: int
    external_id: str
    title: str
    mime_type: str | None
    web_url: str | None
    content_hash: str
    modified_at: datetime | None


@dataclass(frozen=True)
class IngestStats:
    chunks: int
    embedded: int  # vectors computed by the model
    reused: int  # vectors copied from identical chunks already in the index
    seconds: float


def text_hash(chunk_text: str) -> str:
    return hashlib.sha256(chunk_text.encode()).hexdigest()


async def known_embeddings(hashes: list[str]) -> dict[str, str]:
    """Vectors already stored for identical chunk text (any document), read from the primary."""
    if not hashes:
        return {}
    async with primary_engine().connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT DISTINCT ON (text_hash) text_hash, embedding::text "
                "FROM chunks WHERE text_hash = ANY(:hashes)"
            ),
            {"hashes": hashes},
        )
        return {row[0]: row[1] for row in rows}


async def embed_chunks(
    chunks: list[Chunk], provider: EmbeddingProvider
) -> tuple[list[str], list[str], int]:
    """Return (hashes, pgvector literals, number reused) aligned with `chunks`."""
    hashes = [text_hash(c.text) for c in chunks]
    cached = await known_embeddings(list(set(hashes)))

    missing = sorted({h for h in hashes if h not in cached})
    if missing:
        by_hash = {h: c.text for h, c in zip(hashes, chunks, strict=True)}
        vectors = await provider.embed_documents([by_hash[h] for h in missing])
        cached.update({h: to_pgvector(v) for h, v in zip(missing, vectors, strict=True)})

    reused = sum(1 for h in hashes if h not in missing)
    return hashes, [cached[h] for h in hashes], reused


async def ingest_file(
    doc: DocumentInput,
    path: Path,
    provider: EmbeddingProvider,
    settings: Settings,
) -> IngestStats:
    started = time.perf_counter()

    blocks = await asyncio.to_thread(extract, path)  # CPU-bound parsing off the event loop
    chunks = chunk_blocks(blocks, settings.chunk_tokens, settings.chunk_overlap_tokens)
    hashes, vectors, reused = await embed_chunks(chunks, provider)

    async with write_conn() as conn:
        document_id = (
            await conn.execute(
                text("""
                INSERT INTO documents (source_id, external_id, title, mime_type, web_url,
                                       content_hash, modified_at, indexed_at, status, error)
                VALUES (:source_id, :external_id, :title, :mime_type, :web_url,
                        :content_hash, :modified_at, now(), 'indexed', NULL)
                ON CONFLICT (source_id, external_id) DO UPDATE SET
                    title = EXCLUDED.title, mime_type = EXCLUDED.mime_type,
                    web_url = EXCLUDED.web_url, content_hash = EXCLUDED.content_hash,
                    modified_at = EXCLUDED.modified_at, indexed_at = now(),
                    status = 'indexed', error = NULL
                RETURNING id
                """),
                doc.__dict__,
            )
        ).scalar_one()

        await conn.execute(text("DELETE FROM chunks WHERE document_id = :id"), {"id": document_id})
        if chunks:
            await conn.execute(
                text("""
                INSERT INTO chunks (document_id, ordinal, page, heading_path,
                                    char_start, char_end, text, text_hash, embedding)
                VALUES (:document_id, :ordinal, :page, :heading_path,
                        :char_start, :char_end, :text, :text_hash,
                        CAST(CAST(:embedding AS text) AS vector))
                """),
                [
                    {
                        "document_id": document_id,
                        "ordinal": c.ordinal,
                        "page": c.page,
                        "heading_path": c.heading_path,
                        "char_start": c.char_start,
                        "char_end": c.char_end,
                        "text": c.text,
                        "text_hash": h,
                        "embedding": v,
                    }
                    for c, h, v in zip(chunks, hashes, vectors, strict=True)
                ],
            )

    return IngestStats(
        chunks=len(chunks),
        embedded=len(chunks) - reused,
        reused=reused,
        seconds=time.perf_counter() - started,
    )


async def mark_failed(doc: DocumentInput, error: str) -> None:
    async with write_conn() as conn:
        await conn.execute(
            text("""
            INSERT INTO documents (source_id, external_id, title, mime_type, content_hash,
                                   modified_at, status, error)
            VALUES (:source_id, :external_id, :title, :mime_type, :content_hash,
                    :modified_at, 'failed', :error)
            ON CONFLICT (source_id, external_id) DO UPDATE SET
                content_hash = EXCLUDED.content_hash, modified_at = EXCLUDED.modified_at,
                status = 'failed', error = EXCLUDED.error
            """),
            {**doc.__dict__, "error": error[:2000]},
        )


async def bump_index_version() -> int:
    """Invalidates every cached search result at once (cache keys include the version)."""
    async with write_conn() as conn:
        result = await conn.execute(
            text("UPDATE index_state SET version = version + 1 WHERE id = 1 RETURNING version")
        )
        return int(result.scalar_one())
