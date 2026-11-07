"""Ingestion worker: keeps the index in sync with the local docs folder.

Weekend 1 version: a periodic scan that indexes new/changed files and removes
deleted ones. Weekend 3 moves the per-document work onto a Redis Streams queue
so several workers can share it.
"""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from sqlalchemy import text

from app.config import Settings, get_settings
from app.connectors import local
from app.db import dispose_engines, read_conn, write_conn
from app.ingest.embed import EmbeddingProvider, make_provider
from app.ingest.pipeline import DocumentInput, bump_index_version, ingest_file, mark_failed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


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


async def wait_until_ready(provider: EmbeddingProvider, stop: asyncio.Event) -> None:
    delay = 2.0
    while not stop.is_set() and not await provider.ready():
        log.info("waiting for embedding model '%s' (first start downloads it)...", provider.model)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 30)


async def sync_once(
    source_id: int, root: Path, provider: EmbeddingProvider, settings: Settings
) -> int:
    """One pass over the folder. Returns the number of documents changed."""
    async with read_conn() as conn:
        rows = await conn.execute(
            text(
                "SELECT external_id, content_hash, modified_at, status "
                "FROM documents WHERE source_id = :s"
            ),
            {"s": source_id},
        )
        known = {r.external_id: r for r in rows}

    changed = 0
    seen: set[str] = set()
    files = await asyncio.to_thread(lambda: list(local.scan(root)))  # filesystem walk off the loop
    for file in files:
        seen.add(file.external_id)
        previous = known.get(file.external_id)
        if previous and previous.modified_at == file.modified_at:
            continue  # cheap path: untouched since last sync, no need to read the file

        digest = await asyncio.to_thread(local.content_hash, file.path)
        if previous and previous.content_hash == digest and previous.status == "indexed":
            continue  # touched but identical content

        doc = DocumentInput(
            source_id=source_id,
            external_id=file.external_id,
            title=file.title,
            mime_type=file.mime_type,
            web_url=None,
            content_hash=digest,
            modified_at=file.modified_at,
        )
        try:
            stats = await ingest_file(doc, file.path, provider, settings)
            log.info(
                "indexed %s: %d chunks (%d embedded, %d reused) in %.2fs",
                file.external_id,
                stats.chunks,
                stats.embedded,
                stats.reused,
                stats.seconds,
            )
        except Exception as exc:  # one bad file must not stop the sync
            log.exception("failed to index %s", file.external_id)
            await mark_failed(doc, f"{type(exc).__name__}: {exc}")
        changed += 1

    removed = set(known) - seen
    if removed:
        async with write_conn() as conn:
            await conn.execute(
                text("DELETE FROM documents WHERE source_id = :s AND external_id = ANY(:ids)"),
                {"s": source_id, "ids": sorted(removed)},
            )
        log.info("removed %d deleted documents", len(removed))
        changed += len(removed)

    if changed:
        version = await bump_index_version()
        log.info("sync done: %d changes, index version %d", changed, version)
    return changed


async def run() -> None:
    settings = get_settings()
    root = Path(settings.local_docs_dir)
    provider = make_provider(settings)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await wait_until_ready(provider, stop)
        await check_embedding_model(provider)
        source_id = await ensure_local_source()
        log.info("watching %s every %ds", root, settings.sync_interval_seconds)

        while not stop.is_set():
            try:
                if await asyncio.to_thread(root.is_dir):
                    await sync_once(source_id, root, provider, settings)
                else:
                    log.warning("docs folder %s does not exist; set LOCAL_DOCS_PATH", root)
            except Exception:  # e.g. database restarting: log and retry next round
                log.exception("sync failed; retrying in %ds", settings.sync_interval_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.sync_interval_seconds)
    finally:
        await provider.close()
        await dispose_engines()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
