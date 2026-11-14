"""The ingestion queue against a real Redis (Streams, consumer groups, sorted sets)."""

import os
import uuid

import pytest
from redis.asyncio import Redis

from app.queue import IngestQueue, Job, queue_depth

pytestmark = pytest.mark.redis


def job(name: str = "notes/a.md", content_hash: str = "h1") -> Job:
    return Job(
        kind="local",
        source_id=1,
        external_id=name,
        title=name,
        mime_type="text/markdown",
        content_hash=content_hash,
        modified_at="2026-11-14T10:00:00+00:00",
    )


@pytest.fixture
async def redis():
    client = Redis.from_url(os.environ["REDIS_URL"])
    yield client
    await client.aclose()


@pytest.fixture
async def make_queue(redis):
    prefix = f"test:{uuid.uuid4().hex}"

    async def make(consumer: str = "w1", **options) -> IngestQueue:
        queue = IngestQueue(redis, consumer, prefix=prefix, **options)
        await queue.ensure_group()
        return queue

    make.prefix = prefix
    yield make
    keys = [key async for key in redis.scan_iter(f"{prefix}:*")]
    if keys:
        await redis.delete(*keys)


async def test_job_round_trip_and_acknowledge(make_queue, redis):
    queue = await make_queue()
    assert await queue.enqueue(job()) is True

    (message,) = await queue.read(block_ms=10)
    assert message.job == job()
    assert (await queue_depth(redis, make_queue.prefix))["waiting"] == 1  # in flight

    await queue.done(message)
    assert await queue_depth(redis, make_queue.prefix) == {"waiting": 0, "retrying": 0, "dead": 0}
    assert await queue.read(block_ms=10) == []


async def test_same_document_version_is_queued_once(make_queue):
    queue = await make_queue()
    assert await queue.enqueue(job()) is True
    assert await queue.enqueue(job()) is False  # already queued
    assert await queue.enqueue(job(content_hash="h2")) is True  # a newer version is a new job

    (first, second) = await queue.read(block_ms=10)
    await queue.done(first)
    await queue.done(second)
    assert await queue.enqueue(job()) is True  # finished, so it can be queued again


async def test_each_job_goes_to_exactly_one_worker(make_queue):
    worker_a = await make_queue("a")
    worker_b = await make_queue("b")
    for i in range(4):
        await worker_a.enqueue(job(f"doc-{i}.md"))

    got_a = await worker_a.read(count=2, block_ms=10)
    got_b = await worker_b.read(count=10, block_ms=10)
    names_a = {m.job.external_id for m in got_a}
    names_b = {m.job.external_id for m in got_b}
    assert len(names_a) == 2 and len(names_b) == 2
    assert names_a.isdisjoint(names_b)


async def test_failed_job_is_retried_with_the_attempt_counted(make_queue):
    queue = await make_queue(retry_base_seconds=0)
    await queue.enqueue(job())
    (message,) = await queue.read(block_ms=10)

    assert await queue.failed(message, "boom") is True
    (retry,) = await queue.read(block_ms=10)
    assert retry.job.attempt == 1
    assert retry.job.external_id == message.job.external_id


async def test_retry_waits_for_its_backoff(make_queue, redis):
    queue = await make_queue(retry_base_seconds=60)
    await queue.enqueue(job())
    (message,) = await queue.read(block_ms=10)
    await queue.failed(message, "boom")

    assert await queue.read(block_ms=10) == []  # not due for another minute
    assert (await queue_depth(redis, make_queue.prefix))["retrying"] == 1
    assert await queue.enqueue(job()) is False  # and the scheduler can't re-add it meanwhile


async def test_job_goes_to_dead_letters_when_attempts_are_used_up(make_queue, redis):
    queue = await make_queue(max_attempts=2, retry_base_seconds=0)
    await queue.enqueue(job())

    (first,) = await queue.read(block_ms=10)
    assert await queue.failed(first, "boom 1") is True
    (second,) = await queue.read(block_ms=10)
    assert await queue.failed(second, "boom 2") is False

    assert await queue.read(block_ms=10) == []
    assert await queue_depth(redis, make_queue.prefix) == {"waiting": 0, "retrying": 0, "dead": 1}
    (dead,) = await queue.dead_letters()
    assert dead["error"] == "boom 2"
    assert Job.loads(dead["job"]).external_id == "notes/a.md"


async def test_job_of_a_crashed_worker_is_recovered(make_queue):
    crashed = await make_queue("crashed", claim_idle_ms=0, retry_base_seconds=0)
    survivor = await make_queue("survivor", claim_idle_ms=0, retry_base_seconds=0)
    await crashed.enqueue(job())
    await crashed.read(block_ms=10)  # delivered, never acknowledged

    assert await survivor.read(block_ms=10) == []  # recovered and scheduled as a retry
    (message,) = await survivor.read(block_ms=10)
    assert message.job.attempt == 1  # the crash used up one attempt
