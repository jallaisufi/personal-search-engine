"""Scheduler → queue → worker → Postgres, end to end with real Redis and pgvector."""

import os
import uuid

import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.config import get_settings
from app.db import read_conn, write_conn
from app.ingest.embed import FakeProvider
from app.llm import FakeLLM
from app.queue import IngestQueue, queue_depth
from app.scheduler import scan_once
from app.sync import check_embedding_model, ensure_local_source
from app.worker import run_batch

pytestmark = [pytest.mark.integration, pytest.mark.redis, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture
async def stack(tmp_path):
    async with write_conn() as conn:
        await conn.execute(text("TRUNCATE sources, documents, chunks, deadlines CASCADE"))
        await conn.execute(text("UPDATE index_state SET embedding_model = NULL WHERE id = 1"))

    settings = get_settings()
    provider = FakeProvider(settings.embedding_dim)
    await check_embedding_model(provider)
    redis = Redis.from_url(os.environ["REDIS_URL"])
    prefix = f"test:{uuid.uuid4().hex}"
    queue = IngestQueue(redis, "worker-1", prefix=prefix, max_attempts=2, retry_base_seconds=0)
    await queue.ensure_group()

    source_id = await ensure_local_source()

    class Stack:
        root = tmp_path

        async def scan(self) -> int:
            return await scan_once(source_id, tmp_path, queue, redis)

        async def work(self, llm=None) -> int:
            return await run_batch(queue, tmp_path, provider, settings, redis, llm=llm, block_ms=10)

        async def depth(self) -> dict[str, int]:
            return await queue_depth(redis, prefix)

        async def dead_letters(self) -> list[dict[str, str]]:
            return await queue.dead_letters()

    yield Stack()
    keys = [key async for key in redis.scan_iter(f"{prefix}:*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


async def statuses() -> dict[str, str]:
    async with read_conn() as conn:
        rows = await conn.execute(text("SELECT external_id, status FROM documents"))
        return {r.external_id: r.status for r in rows}


async def index_version() -> int:
    async with read_conn() as conn:
        return (await conn.execute(text("SELECT version FROM index_state"))).scalar_one()


async def test_changed_files_flow_through_the_queue(stack):
    (stack.root / "a.md").write_text("# Rent\n\nRent is due on the third working day.")
    (stack.root / "b.txt").write_text("Submit the tax return by 31 July.")
    version_before = await index_version()

    assert await stack.scan() == 2
    assert await statuses() == {"a.md": "pending", "b.txt": "pending"}
    assert await stack.scan() == 0  # already queued: no duplicates

    assert await stack.work() == 2
    assert await statuses() == {"a.md": "indexed", "b.txt": "indexed"}
    assert await index_version() == version_before + 1  # once per batch
    assert await stack.depth() == {"waiting": 0, "retrying": 0, "dead": 0}

    assert await stack.scan() == 0  # nothing changed
    edited = stack.root / "a.md"
    edited.write_text("# Rent\n\nRent is now due on the first of the month.")
    later = edited.stat().st_mtime + 5  # don't depend on the filesystem's timestamp precision
    os.utime(edited, (later, later))
    assert await stack.scan() == 1  # only the edited file


async def test_unreadable_file_is_retried_then_marked_failed(stack):
    (stack.root / "broken.docx").write_bytes(b"this is not a Word file")
    (stack.root / "fine.md").write_text("A perfectly readable note.")

    assert await stack.scan() == 2
    assert await stack.work() == 2  # fine.md indexed; broken.docx fails, retry scheduled
    assert (await statuses())["fine.md"] == "indexed"
    assert (await statuses())["broken.docx"] == "pending"

    assert await stack.work() == 1  # second and last attempt
    assert (await statuses())["broken.docx"] == "failed"
    assert (await stack.depth())["dead"] == 1
    (dead,) = await stack.dead_letters()
    assert "broken.docx" in dead["job"]

    assert await stack.scan() == 0  # not retried again until the file changes


async def test_deleted_files_leave_the_index(stack):
    (stack.root / "gone.md").write_text("Temporary note.")
    await stack.scan()
    await stack.work()
    assert "gone.md" in await statuses()

    (stack.root / "gone.md").unlink()
    await stack.scan()
    assert "gone.md" not in await statuses()


async def test_deadlines_are_extracted_by_a_follow_up_job(stack):
    (stack.root / "lease.md").write_text("# Lease\n\nSend the signed lease by 1 December 2026.")
    llm = FakeLLM()

    await stack.scan()
    assert await stack.work(llm) == 1  # the indexing job, which queues the deadlines job
    assert (await stack.depth())["waiting"] == 1
    assert await stack.work(llm) == 1  # the deadlines job
    assert await stack.depth() == {"waiting": 0, "retrying": 0, "dead": 0}

    async with read_conn() as conn:
        rows = (await conn.execute(text("SELECT due_on, quote FROM deadlines"))).all()
    assert [(str(r.due_on), r.quote) for r in rows] == [
        ("2026-12-01", "Send the signed lease by 1 December 2026.")
    ]
