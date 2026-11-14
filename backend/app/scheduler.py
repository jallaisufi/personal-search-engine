"""Scheduler: watches sources (local folder, Google Drive) and queues one job per
new or changed document.

Run exactly one scheduler; run as many workers as you like. The scheduler does the
cheap part (listing files, comparing hashes); workers do the expensive part
(parsing and embedding).
"""

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from redis.asyncio import Redis

from app.config import get_settings
from app.connectors.gdrive import GoogleError
from app.crypto import DecryptionError
from app.db import dispose_engines
from app.ingest.pipeline import mark_pending
from app.queue import IngestQueue
from app.sync import (
    DriveRegistry,
    DriveSource,
    ensure_local_source,
    gdrive_sources,
    job_document,
    make_drives,
    plan_gdrive,
    plan_local,
    publish_changes,
    record_sync,
    remove_documents,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduler")


async def scan_once(source_id: int, root: Path, queue: IngestQueue, redis: Redis | None) -> int:
    """Queue changed documents and drop deleted ones. Returns the number of jobs queued."""
    jobs, removed = await plan_local(source_id, root)

    queued = 0
    for job in jobs:
        await mark_pending(job_document(job))  # shows as "Waiting" until a worker indexes it
        if await queue.enqueue(job):
            queued += 1

    if removed:
        await remove_documents(source_id, removed)
        await publish_changes(redis)
    if queued or removed:
        log.info("queued %d documents, removed %d", queued, len(removed))
    return queued


async def scan_gdrive(
    source: DriveSource,
    queue: IngestQueue,
    redis: Redis | None,
    drives: DriveRegistry,
    max_bytes: int,
) -> int:
    """Queue what changed in one Drive account. Returns the number of jobs queued."""
    try:
        drive = drives.client(source.credentials)
        jobs, removed, cursor = await plan_gdrive(source, drive, max_bytes)
    except (GoogleError, DecryptionError) as exc:
        # Shown next to the account in the UI ("Reconnect"); other sources keep syncing.
        log.warning("Google Drive sync failed for %s: %s", source.name, exc)
        await record_sync(source.id, None, str(exc))
        return 0

    queued = 0
    for job in jobs:
        await mark_pending(job_document(job))
        if await queue.enqueue(job):
            queued += 1
    if removed:
        await remove_documents(source.id, removed)
        await publish_changes(redis)

    # Saved last: if anything above fails, the next sync starts from the old cursor again.
    await record_sync(source.id, cursor, None)
    if queued or removed:
        log.info("%s: queued %d documents, removed %d", source.name, queued, len(removed))
    return queued


async def run() -> None:
    settings = get_settings()
    root = Path(settings.local_docs_dir)
    redis = Redis.from_url(settings.redis_url, socket_timeout=10)
    queue = IngestQueue(redis, consumer="scheduler")
    drives = make_drives(settings)
    max_bytes = settings.drive_max_file_mb * 1024 * 1024

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await queue.ensure_group()
        source_id = await ensure_local_source()
        log.info("watching %s every %ds", root, settings.sync_interval_seconds)
        while not stop.is_set():
            try:
                if await asyncio.to_thread(root.is_dir):
                    await scan_once(source_id, root, queue, redis)
                else:
                    log.warning("docs folder %s does not exist; set LOCAL_DOCS_PATH", root)
                if drives is not None:
                    for source in await gdrive_sources():
                        await scan_gdrive(source, queue, redis, drives, max_bytes)
            except Exception:  # e.g. database or Redis restarting: log and retry next round
                log.exception("scan failed; retrying in %ds", settings.sync_interval_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.sync_interval_seconds)
    finally:
        if drives is not None:
            await drives.oauth.close()
        await redis.aclose()
        await dispose_engines()
        log.info("scheduler stopped")


if __name__ == "__main__":
    asyncio.run(run())
