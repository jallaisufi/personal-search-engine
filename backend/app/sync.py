"""Shared sync logic: work out what changed in a source and index one document.

Used by the scheduler (plans changes and queues jobs), the worker (runs jobs),
and `sync_once` (does both inline, for tests and one-off runs).
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import text

from app.cache import publish_index_version
from app.config import Settings
from app.connectors import local
from app.db import read_conn, write_conn
from app.ingest.embed import EmbeddingProvider
from app.ingest.pipeline import (
    DocumentInput,
    IngestStats,
    bump_index_version,
    ingest_file,
    mark_failed,
)
from app.queue import Job

log = logging.getLogger(__name__)


class EmbeddingModelMismatch(RuntimeError):
    pass


async def ensure_local_source() -> int:
    async with write_conn() as conn:
        result = await conn.execute(
            text("""
            INSERT INTO sources (kind, name) VALUES ('local', 'local')
            ON CONFLICT (kind, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """)
        )
        return int(result.scalar_one())


async def check_embedding_model(provider: EmbeddingProvider) -> None:
    """Refuse to mix vectors from different models in one index."""
    async with write_conn() as conn:
        stored = (
            await conn.execute(text("SELECT embedding_model FROM index_state WHERE id = 1"))
        ).scalar_one_or_none()
        if stored is None:
            await conn.execute(
                text("UPDATE index_state SET embedding_model = :m WHERE id = 1"),
                {"m": provider.model},
            )
        elif stored != provider.model:
            raise EmbeddingModelMismatch(
                f"Index was built with '{stored}' but EMBEDDING_MODEL is '{provider.model}'. "
                "Run `make reindex` to rebuild the index with the new model."
            )


def job_document(job: Job) -> DocumentInput:
    return DocumentInput(
        source_id=job.source_id,
        external_id=job.external_id,
        title=job.title,
        mime_type=job.mime_type,
        web_url=None,
        content_hash=job.content_hash,
        modified_at=datetime.fromisoformat(job.modified_at) if job.modified_at else None,
    )


async def plan_local(source_id: int, root: Path) -> tuple[list[Job], list[str]]:
    """Compare the folder with the index. Returns (documents to index, ids to remove).

    Cheap by design: a file whose modification time is unchanged is skipped without
    being read; otherwise its content hash decides whether anything really changed.
    """
    async with read_conn() as conn:
        rows = await conn.execute(
            text(
                "SELECT external_id, content_hash, modified_at, status "
                "FROM documents WHERE source_id = :s"
            ),
            {"s": source_id},
        )
        known = {r.external_id: r for r in rows}

    jobs: list[Job] = []
    seen: set[str] = set()
    files = await asyncio.to_thread(lambda: list(local.scan(root)))  # filesystem walk off the loop
    for file in files:
        seen.add(file.external_id)
        previous = known.get(file.external_id)
        waiting = previous is not None and previous.status == "pending"
        if previous and previous.modified_at == file.modified_at and not waiting:
            continue  # untouched since it was indexed (or failed): no need to read the file

        digest = await asyncio.to_thread(local.content_hash, file.path)
        if previous and previous.content_hash == digest and previous.status == "indexed":
            continue  # touched but identical content

        jobs.append(
            Job(
                kind="local",
                source_id=source_id,
                external_id=file.external_id,
                title=file.title,
                mime_type=file.mime_type,
                content_hash=digest,
                modified_at=file.modified_at.isoformat(),
            )
        )
    return jobs, sorted(set(known) - seen)


async def remove_documents(source_id: int, external_ids: list[str]) -> None:
    if not external_ids:
        return
    async with write_conn() as conn:
        await conn.execute(
            text("DELETE FROM documents WHERE source_id = :s AND external_id = ANY(:ids)"),
            {"s": source_id, "ids": external_ids},
        )
    log.info("removed %d deleted documents", len(external_ids))


def _locate(root: Path, external_id: str) -> Path | None:
    """The file for a job, or None if it no longer exists. Never leaves the documents folder."""
    path = (root / external_id).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"{external_id} is outside the documents folder")
    return path if path.is_file() else None


async def index_job(
    job: Job, root: Path, provider: EmbeddingProvider, settings: Settings
) -> IngestStats | None:
    """Index one document. Returns None when the file no longer exists. Raises on failure."""
    path = await asyncio.to_thread(_locate, root, job.external_id)
    if path is None:
        return None  # deleted since it was queued; the scheduler removes it from the index

    stats = await ingest_file(job_document(job), path, provider, settings)
    log.info(
        "indexed %s: %d chunks (%d embedded, %d reused) in %.2fs",
        job.external_id,
        stats.chunks,
        stats.embedded,
        stats.reused,
        stats.seconds,
    )
    return stats


async def publish_changes(redis: Redis | None) -> int:
    """Bump the index version so every cached search result is retired."""
    version = await bump_index_version()
    if redis is not None:
        await publish_index_version(redis, version)
    return version


async def sync_once(
    source_id: int,
    root: Path,
    provider: EmbeddingProvider,
    settings: Settings,
    redis: Redis | None = None,
) -> int:
    """Plan and index inline, without the queue. Returns the number of documents changed."""
    jobs, removed = await plan_local(source_id, root)
    for job in jobs:
        try:
            await index_job(job, root, provider, settings)
        except Exception as exc:  # one bad file must not stop the sync
            log.exception("failed to index %s", job.external_id)
            await mark_failed(job_document(job), f"{type(exc).__name__}: {exc}")
    await remove_documents(source_id, removed)

    changed = len(jobs) + len(removed)
    if changed:
        version = await publish_changes(redis)
        log.info("sync done: %d changes, index version %d", changed, version)
    return changed
