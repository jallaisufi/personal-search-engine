"""End-to-end against real Postgres + pgvector: index the sample docs, then search.

Run locally:  docker compose up -d postgres && make test
(with DATABASE_URL=postgresql+asyncpg://app:app@localhost:5432/search and migrations applied)
"""

import shutil

import httpx
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import read_conn, write_conn
from app.ingest.embed import FakeProvider
from app.main import app
from app.worker import check_embedding_model, ensure_local_source, sync_once

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


@pytest.fixture(scope="module")
async def indexed(tmp_path_factory, sample_docs):
    async with write_conn() as conn:
        await conn.execute(text("TRUNCATE sources, documents, chunks, deadlines CASCADE"))
        await conn.execute(text("UPDATE index_state SET embedding_model = NULL WHERE id = 1"))

    root = tmp_path_factory.mktemp("docs")
    shutil.copytree(sample_docs, root, dirs_exist_ok=True)

    settings = get_settings()
    provider = FakeProvider(settings.embedding_dim)
    await check_embedding_model(provider)
    source_id = await ensure_local_source()
    changed = await sync_once(source_id, root, provider, settings)
    return {"root": root, "source_id": source_id, "provider": provider, "changed": changed}


@pytest.fixture(scope="module")
async def client(indexed):
    app.state.embedder = indexed["provider"]
    app.state.redis = FakeRedis()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class FakeRedis:
    async def ping(self) -> bool:
        return True


async def test_all_sample_documents_indexed(indexed, sample_docs):
    expected = sum(1 for p in sample_docs.rglob("*") if p.is_file())
    async with read_conn() as conn:
        rows = (
            await conn.execute(text("SELECT status, count(*) FROM documents GROUP BY status"))
        ).all()
    assert dict(rows) == {"indexed": expected}


async def test_search_finds_the_right_document_and_location(client):
    resp = await client.get("/search", params={"q": "security deposit"})
    assert resp.status_code == 200
    top = resp.json()["results"][0]
    assert top["path"] == "apartment/lease-agreement.md"
    assert top["heading_path"] == "Residential Lease Agreement > Security Deposit"
    assert "<mark>" in top["snippet_html"]
    assert set(top["matched_by"]) == {"semantic", "keyword"}


async def test_exact_token_match(client):
    resp = await client.get("/search", params={"q": "mTLS"})
    assert resp.json()["results"][0]["path"] == "work/project-atlas-kickoff.md"


async def test_pdf_results_carry_page_numbers(client):
    resp = await client.get("/search", params={"q": "pgvector production embeddings talk"})
    pdf_hits = [r for r in resp.json()["results"] if r["path"].endswith(".pdf")]
    assert pdf_hits and pdf_hits[0]["page"] == 2


async def test_snippets_are_html_safe(indexed, client):
    evil = indexed["root"] / "evil.md"
    evil.write_text("# Note\n\n<script>alert('xss')</script> quarterly budget")
    settings = get_settings()
    await sync_once(indexed["source_id"], indexed["root"], indexed["provider"], settings)

    resp = await client.get("/search", params={"q": "quarterly budget"})
    hit = next(r for r in resp.json()["results"] if r["path"] == "evil.md")
    assert "<script>" not in hit["snippet_html"]
    assert "<mark>" in hit["snippet_html"]


async def test_resync_without_changes_is_a_noop(indexed):
    settings = get_settings()
    changed = await sync_once(indexed["source_id"], indexed["root"], indexed["provider"], settings)
    assert changed == 0


async def test_identical_chunks_reuse_embeddings(indexed):
    from app.ingest.pipeline import DocumentInput, ingest_file

    src = indexed["root"] / "apartment" / "lease-agreement.md"
    copy = indexed["root"] / "apartment" / "lease-copy.md"
    shutil.copy(src, copy)
    stats = await ingest_file(
        DocumentInput(
            indexed["source_id"], "apartment/lease-copy.md", copy.name, None, None, "x", None
        ),
        copy,
        indexed["provider"],
        get_settings(),
    )
    assert stats.chunks > 0
    assert stats.embedded == 0 and stats.reused == stats.chunks


async def test_deleted_files_are_removed(indexed, client):
    (indexed["root"] / "finance" / "tax-return-checklist.txt").unlink()
    settings = get_settings()
    await sync_once(indexed["source_id"], indexed["root"], indexed["provider"], settings)
    resp = await client.get("/search", params={"q": "ELSTER tax return"})
    assert all(r["path"] != "finance/tax-return-checklist.txt" for r in resp.json()["results"])


async def test_health_and_stats(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    stats = (await client.get("/stats")).json()
    assert stats["chunks"] > 0 and stats["index_version"] > 0
    assert stats["embedding_model"] == "fake-hash"


async def test_document_detail_locates_the_hit(client):
    hit = (await client.get("/search", params={"q": "security deposit"})).json()["results"][0]
    assert 0 < hit["char_end"] <= hit["doc_length"]

    doc = (await client.get(f"/documents/{hit['document_id']}")).json()
    assert doc["path"] == hit["path"]
    assert "security deposit" in doc["text"][hit["char_start"] : hit["char_end"]].lower()
    assert any(c["id"] == hit["chunk_id"] for c in doc["chunks"])


async def test_unknown_document_is_404(client):
    assert (await client.get("/documents/999999999")).status_code == 404
