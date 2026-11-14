"""Ingestion worker: takes jobs from the queue and indexes documents.

Workers are interchangeable and stateless. Start more of them (WORKER_REPLICAS)
to index faster; the consumer group hands each job to exactly one worker.
"""

import asyncio
import logging
import os
import signal
import socket
from pathlib import Path

from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import dispose_engines
from app.ingest.embed import EmbeddingProvider, make_provider
from app.ingest.pipeline import mark_failed
from app.queue import IngestQueue, Message
from app.sync import check_embedding_model, index_job, job_document, publish_changes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")


async def wait_until_ready(provider: EmbeddingProvider, stop: asyncio.Event) -> None:
    delay = 2.0
    while not stop.is_set() and not await provider.ready():
        log.info("waiting for embedding model '%s' (first start downloads it)...", provider.model)
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 30)


async def handle(
    queue: IngestQueue,
    message: Message,
    root: Path,
    provider: EmbeddingProvider,
    settings: Settings,
) -> bool:
    """Run one job. Returns True when the index changed."""
    job = message.job
    try:
        stats = await index_job(job, root, provider, settings)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        attempt = job.attempt + 1
        if await queue.failed(message, error):
            log.warning(
                "indexing %s failed (attempt %d), will retry: %s", job.external_id, attempt, error
            )
        else:
            log.error("giving up on %s after %d attempts: %s", job.external_id, attempt, error)
            await mark_failed(job_document(job), error)
        return False

    await queue.done(message)
    return stats is not None


async def run_batch(
    queue: IngestQueue,
    root: Path,
    provider: EmbeddingProvider,
    settings: Settings,
    redis: Redis | None,
    *,
    block_ms: int = 2000,
) -> int:
    """Read and run one batch of jobs. Returns how many jobs were read."""
    messages = await queue.read(count=settings.ingest_batch_size, block_ms=block_ms)
    changed = 0
    for message in messages:
        changed += await handle(queue, message, root, provider, settings)
    if changed:
        version = await publish_changes(redis)  # once per batch, not once per document
        log.info("batch done: %d documents indexed, index version %d", changed, version)
    return len(messages)


async def run() -> None:
    settings = get_settings()
    root = Path(settings.local_docs_dir)
    provider = make_provider(settings)
    redis = Redis.from_url(settings.redis_url, socket_timeout=10)
    queue = IngestQueue(
        redis,
        consumer=f"{socket.gethostname()}-{os.getpid()}",
        max_attempts=settings.ingest_max_attempts,
        retry_base_seconds=settings.ingest_retry_base_seconds,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await wait_until_ready(provider, stop)
        await check_embedding_model(provider)
        await queue.ensure_group()
        log.info("worker %s ready", queue.consumer)

        while not stop.is_set():
            try:
                await run_batch(queue, root, provider, settings, redis)
            except Exception:  # e.g. Redis or the database restarting
                log.exception("batch failed; retrying shortly")
                await asyncio.sleep(2)
    finally:
        await provider.close()
        await redis.aclose()
        await dispose_engines()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
