"""Deadline extraction end to end on the sample documents (real Postgres, fake model)."""

import os
import shutil
from datetime import date

import httpx
import pytest
from sqlalchemy import text

from app.cache import SearchCache
from app.config import get_settings
from app.db import write_conn
from app.deadlines import extract_deadlines
from app.ingest.embed import FakeProvider
from app.llm import FakeLLM
from app.main import app
from app.sync import check_embedding_model, ensure_local_source, sync_once
from tests.fakes import InMemoryRedis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

SAMPLE_DEADLINES = [
    "2026-11-20",  # design review (project notes)
    "2026-11-27",  # staging cluster
    "2026-12-01",  # first rent payment (lease)
    "2026-12-04",  # mTLS plan
    "2026-12-15",  # literature review (thesis)
    "2027-02-28",  # "end of February 2027"
    "2027-03-31",  # free hotel cancellation (PDF)
    "2027-06-30",  # thesis submission
    "2027-07-31",  # tax return
]


@pytest.fixture(scope="module")
async def stack(tmp_path_factory, sample_docs):
    async with write_conn() as conn:
        await conn.execute(text("TRUNCATE sources, documents, chunks, deadlines CASCADE"))
        await conn.execute(text("UPDATE index_state SET embedding_model = NULL WHERE id = 1"))

    root = tmp_path_factory.mktemp("docs")
    shutil.copytree(sample_docs, root, dirs_exist_ok=True)
    settings = get_settings()
    provider = FakeProvider(settings.embedding_dim)
    redis = InMemoryRedis()
    llm = FakeLLM()
    await check_embedding_model(provider)
    source_id = await ensure_local_source()

    app.state.embedder = provider
    app.state.llm = llm
    app.state.redis = redis
    app.state.cache = SearchCache(redis, search_ttl_seconds=600)

    class Stack:
        def __init__(self) -> None:
            self.root = root
            self.source_id = source_id
            self.llm = llm
            self.client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )

        async def sync(self) -> int:
            return await sync_once(source_id, root, provider, settings, redis, llm)

        async def deadlines(self, **params) -> list[dict]:
            return (await self.client.get("/deadlines", params=params)).json()

    stack = Stack()
    await stack.sync()
    yield stack
    await stack.client.aclose()


async def test_sample_deadlines_are_found_once_each(stack):
    found = await stack.deadlines(include_past="true")
    assert [d["due_on"] for d in found] == SAMPLE_DEADLINES  # sorted, no duplicates


async def test_a_deadline_points_at_its_exact_sentence(stack):
    review = next(
        d for d in await stack.deadlines(include_past="true") if d["due_on"] == "2026-11-20"
    )
    assert review["title"] == "project-atlas-kickoff.md"
    assert review["heading_path"] == "Project Atlas — Kickoff Notes > Action Items"
    assert review["quote"] == "Priya: architecture design doc, design review by 20 November 2026."
    assert review["days_left"] == (date(2026, 11, 20) - date.today()).days

    document = (await stack.client.get(f"/documents/{review['document_id']}")).json()
    assert document["text"][review["char_start"] : review["char_end"]] == review["quote"]


async def test_events_without_a_deadline_cue_never_reach_the_model(stack):
    quotes = " ".join(d["quote"] for d in await stack.deadlines(include_past="true"))
    assert "14 April 2027" not in quotes  # the train departure is an event, not a deadline

    stats = (await stack.client.get("/stats")).json()["deadline_extraction"]
    assert 0 < stats["passages_sent_to_model"] < stats["passages_scanned"]
    assert stats["share_sent_to_model"] < 1


async def test_past_deadlines_are_hidden_by_default(stack):
    (stack.root / "old-invoice.md").write_text("Pay the invoice by 1 January 2020.")
    await stack.sync()
    assert "2020-01-01" not in [d["due_on"] for d in await stack.deadlines()]
    past = next(
        d for d in await stack.deadlines(include_past="true") if d["due_on"] == "2020-01-01"
    )
    assert past["days_left"] < 0


async def test_dismissed_deadlines_stay_dismissed_after_reindexing(stack):
    lease = stack.root / "apartment" / "lease-agreement.md"
    rent = next(
        d for d in await stack.deadlines(include_past="true") if d["due_on"] == "2026-12-01"
    )

    assert (await stack.client.post(f"/deadlines/{rent['id']}/dismiss")).status_code == 204
    assert "2026-12-01" not in [d["due_on"] for d in await stack.deadlines(include_past="true")]

    lease.write_text(
        lease.read_text()
        + "\n\n## Insurance\n\nSend proof of liability insurance by 15 January 2027.\n"
    )
    later = lease.stat().st_mtime + 5  # don't depend on the filesystem's timestamp precision
    os.utime(lease, (later, later))
    await stack.sync()  # the document changed and is indexed again
    due = [d["due_on"] for d in await stack.deadlines(include_past="true")]
    assert "2027-01-15" in due  # the new deadline appears
    assert "2026-12-01" not in due  # the dismissed one does not come back

    assert (await stack.client.post("/deadlines/999999999/dismiss")).status_code == 404


async def test_deadlines_disappear_with_their_document(stack):
    (stack.root / "finance" / "tax-return-checklist.txt").unlink()
    await stack.sync()
    assert "2027-07-31" not in [d["due_on"] for d in await stack.deadlines(include_past="true")]


async def test_extraction_of_a_missing_document_is_a_noop(stack):
    assert await extract_deadlines(stack.source_id, "does/not/exist.md", stack.llm) is None
