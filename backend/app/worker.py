"""Ingestion worker: takes jobs from the queue and indexes documents.

Workers are interchangeable and stateless. Start more of them (WORKER_REPLICAS)
to index faster; the consumer group hands each job to exactly one worker.
"""

import asyncio
import logging
import os
import signal
import socket
from dataclasses import replace
from pathlib import Path

from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import dispose_engines
from app.deadlines import extract_deadlines
from app.ingest.embed import EmbeddingProvider, make_provider
from app.ingest.pipeline import mark_failed
from app.llm import LLMProvider, make_llm
from app.queue import IngestQueue, Message
from app.sync import (
    DriveRegistry,
    check_embedding_model,
    index_job,
    job_document,
    make_drives,
    publish_changes,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("worker")

# Backoff 30 s, 2 min, 8 min, 32 min, ~2 h: enough for a first-start model download.
DEADLINE_ATTEMPTS = 6


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
    drives: DriveRegistry | None = None,
    llm: LLMProvider | None = None,
    redis: Redis | None = None,
) -> bool:
    """Run one job. Returns True when the index changed."""
    job = message.job
    if job.task == "deadlines":
        await handle_deadlines(queue, message, llm, redis)
        return False

    try:
        stats = await index_job(job, root, provider, settings, drives)
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
    if stats is None:
        return False
    if llm is not None:  # follow-up job: look for deadlines in the freshly indexed document
        await queue.enqueue(replace(job, task="deadlines", attempt=0))
    return True


async def handle_deadlines(
    queue: IngestQueue, message: Message, llm: LLMProvider | None, redis: Redis | None
) -> None:
    job = message.job
    if llm is None:
        await queue.done(message)
        return
    try:
        await extract_deadlines(job.source_id, job.external_id, llm, redis)
    except Exception as exc:
        # The document is already searchable; only its deadlines are missing. The chat model
        # may still be downloading on a first start, so this is retried for longer.
        error = f"{type(exc).__name__}: {exc}"
        if await queue.failed(message, error, max_attempts=DEADLINE_ATTEMPTS):
            log.warning("deadlines for %s not extracted yet, will retry: %s", job.title, error)
        else:
            log.error("giving up on deadlines for %s: %s", job.title, error)
        return
    await queue.done(message)


async def run_batch(
    queue: IngestQueue,
    root: Path,
    provider: EmbeddingProvider,
    settings: Settings,
    redis: Redis | None,
    *,
    drives: DriveRegistry | None = None,
    llm: LLMProvider | None = None,
    block_ms: int = 2000,
) -> int:
    """Read and run one batch of jobs. Returns how many jobs were read."""
    messages = await queue.read(count=settings.ingest_batch_size, block_ms=block_ms)
    changed = 0
    for message in messages:
        changed += await handle(queue, message, root, provider, settings, drives, llm, redis)
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

    drives = make_drives(settings)
    llm = make_llm(settings)

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
                await run_batch(queue, root, provider, settings, redis, drives=drives, llm=llm)
            except Exception:  # e.g. Redis or the database restarting
                log.exception("batch failed; retrying shortly")
                await asyncio.sleep(2)
    finally:
        await provider.close()
        await llm.close()
        if drives is not None:
            await drives.oauth.close()
        await redis.aclose()
        await dispose_engines()
        log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(run())
