"""Scheduler: watches sources and queues one job per new or changed document.

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
from app.db import dispose_engines
from app.ingest.pipeline import mark_pending
from app.queue import IngestQueue
from app.sync import (
    ensure_local_source,
    job_document,
    plan_local,
    publish_changes,
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


async def run() -> None:
    settings = get_settings()
    root = Path(settings.local_docs_dir)
    redis = Redis.from_url(settings.redis_url, socket_timeout=10)
    queue = IngestQueue(redis, consumer="scheduler")

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
            except Exception:  # e.g. database or Redis restarting: log and retry next round
                log.exception("scan failed; retrying in %ds", settings.sync_interval_seconds)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.sync_interval_seconds)
    finally:
        await redis.aclose()
        await dispose_engines()
        log.info("scheduler stopped")


if __name__ == "__main__":
    asyncio.run(run())
