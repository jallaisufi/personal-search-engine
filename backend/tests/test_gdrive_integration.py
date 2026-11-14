"""Google Drive end to end: connect (OAuth) → sync → queue → worker → search.

Real Postgres and Redis; Google is the in-memory fake.
"""

import os
import uuid

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.cache import SearchCache
from app.config import get_settings
from app.connectors.gdrive import GOOGLE_DOC, GoogleOAuth
from app.crypto import Cipher
from app.db import read_conn, write_conn
from app.ingest.embed import FakeProvider
from app.llm import FakeLLM
from app.main import app
from app.queue import IngestQueue
from app.scheduler import scan_gdrive
from app.sync import DriveRegistry, check_embedding_model, gdrive_sources
from app.worker import run_batch
from tests.fakes import FakeGoogle

pytestmark = [pytest.mark.integration, pytest.mark.redis, pytest.mark.asyncio(loop_scope="session")]

MAX_BYTES = 1024 * 1024


@pytest.fixture
async def drive_stack(tmp_path):
    async with write_conn() as conn:
        await conn.execute(text("TRUNCATE sources, documents, chunks, deadlines CASCADE"))
        await conn.execute(text("UPDATE index_state SET embedding_model = NULL WHERE id = 1"))

    settings = get_settings()
    provider = FakeProvider(settings.embedding_dim)
    await check_embedding_model(provider)

    google = FakeGoogle()
    oauth = GoogleOAuth(
        "client-id",
        "client-secret",
        "http://localhost:8080/api/sources/gdrive/callback",
        transport=httpx.MockTransport(google.handle),
    )
    cipher = Cipher("integration-test-encryption-key")
    drives = DriveRegistry(oauth, cipher)
    redis = Redis.from_url(os.environ["REDIS_URL"])
    prefix = f"test:{uuid.uuid4().hex}"
    queue = IngestQueue(redis, "worker-1", prefix=prefix, max_attempts=2, retry_base_seconds=0)
    await queue.ensure_group()

    app.state.embedder = provider
    app.state.llm = FakeLLM()
    app.state.redis = redis
    app.state.cache = SearchCache(redis, search_ttl_seconds=600)
    app.state.google = oauth
    app.state.cipher = cipher

    class Stack:
        def __init__(self) -> None:
            self.google = google
            self.cipher = cipher
            self.client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            )

        async def connect(self) -> httpx.Response:
            """Go through the sign-in flow the way a browser would."""
            start = await self.client.get("/sources/gdrive/connect")
            state = httpx.URL(start.headers["location"]).params["state"]
            return await self.client.get(
                "/sources/gdrive/callback", params={"code": "auth-code", "state": state}
            )

        async def sync(self) -> int:
            queued = 0
            for source in await gdrive_sources():
                queued += await scan_gdrive(source, queue, redis, drives, MAX_BYTES)
            return queued

        async def work(self) -> int:
            return await run_batch(
                queue, tmp_path, provider, settings, redis, drives=drives, block_ms=10
            )

    stack = Stack()
    yield stack
    await stack.client.aclose()
    keys = [key async for key in redis.scan_iter(f"{prefix}:*")]
    keys += [key async for key in redis.scan_iter("search:*")]
    keys += [key async for key in redis.scan_iter("index:version")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


async def documents() -> dict[str, str]:
    async with read_conn() as conn:
        rows = await conn.execute(text("SELECT title, status FROM documents"))
        return {r.title: r.status for r in rows}


async def test_connect_stores_an_encrypted_refresh_token(drive_stack):
    start = await drive_stack.client.get("/sources/gdrive/connect")
    assert start.status_code == 307
    assert start.headers["location"].startswith("https://accounts.google.com/")

    finish = await drive_stack.connect()
    assert finish.status_code == 307
    assert finish.headers["location"] == "/documents?drive=connected"

    (source,) = await gdrive_sources()
    assert source.name == "me@example.com"
    assert b"refresh-token-1" not in source.credentials  # never stored in plain text
    assert drive_stack.cipher.decrypt(source.credentials) == {"refresh_token": "refresh-token-1"}

    listed = (await drive_stack.client.get("/sources")).json()
    assert listed["google_drive_configured"] is True
    assert [s["name"] for s in listed["sources"] if s["kind"] == "gdrive"] == ["me@example.com"]


async def test_callback_rejects_unknown_and_reused_state(drive_stack):
    forged = await drive_stack.client.get(
        "/sources/gdrive/callback", params={"code": "auth-code", "state": "made-up"}
    )
    assert forged.headers["location"] == "/documents?drive=expired"
    assert await gdrive_sources() == []

    start = await drive_stack.client.get("/sources/gdrive/connect")
    state = httpx.URL(start.headers["location"]).params["state"]
    params = {"code": "auth-code", "state": state}
    first = await drive_stack.client.get("/sources/gdrive/callback", params=params)
    replay = await drive_stack.client.get("/sources/gdrive/callback", params=params)
    assert first.headers["location"] == "/documents?drive=connected"
    assert replay.headers["location"] == "/documents?drive=expired"  # state is single use

    declined = await drive_stack.client.get(
        "/sources/gdrive/callback", params={"error": "access_denied"}
    )
    assert declined.headers["location"] == "/documents?drive=declined"


async def test_drive_files_are_synced_indexed_and_searchable(drive_stack):
    google = drive_stack.google
    google.put(
        "doc-1",
        "Lisbon trip",
        GOOGLE_DOC,
        b"# Lisbon trip\n\n## Flights\n\nThe flight lands at 9am.",
    )
    google.put("txt-1", "packing.txt", "text/plain", b"Pack the charger and the passport.")
    google.put("img-1", "photo.png", "image/png", b"\x89PNG")  # unsupported type: ignored
    google.put("big-1", "huge.txt", "text/plain", b"x" * (MAX_BYTES + 1))  # over the size limit
    await drive_stack.connect()

    assert await drive_stack.sync() == 2
    assert await documents() == {"Lisbon trip": "pending", "packing.txt": "pending"}
    assert await drive_stack.work() == 2
    assert await documents() == {"Lisbon trip": "indexed", "packing.txt": "indexed"}

    top = (
        await drive_stack.client.get("/search", params={"q": "when does the flight land"})
    ).json()["results"][0]
    assert top["title"] == "Lisbon trip"
    assert top["heading_path"] == "Lisbon trip > Flights"  # Markdown export keeps headings
    assert top["web_url"] == "https://drive.google.com/file/d/doc-1/view"
    assert top["path"] == "Google Drive (me@example.com)"


async def test_later_syncs_only_touch_what_changed(drive_stack):
    google = drive_stack.google
    google.put("doc-1", "Notes", GOOGLE_DOC, b"# Notes\n\nFirst version.")
    google.put("txt-1", "todo.txt", "text/plain", b"Buy milk.")
    await drive_stack.connect()
    await drive_stack.sync()
    await drive_stack.work()

    listings = google.requests.count("GET /drive/v3/files")
    assert await drive_stack.sync() == 0  # nothing changed

    google.touch("txt-1")  # opened or renamed: same content
    assert await drive_stack.sync() == 0

    google.put("doc-1", "Notes", GOOGLE_DOC, b"# Notes\n\nSecond version, about budgets.")
    assert await drive_stack.sync() == 1  # only the edited document
    await drive_stack.work()
    hit = (await drive_stack.client.get("/search", params={"q": "budgets"})).json()["results"][0]
    assert hit["title"] == "Notes"

    google.remove("txt-1")
    await drive_stack.sync()
    assert await documents() == {"Notes": "indexed"}
    assert google.requests.count("GET /drive/v3/files") == listings  # no second full listing


async def test_revoked_access_is_reported_and_reconnecting_clears_it(drive_stack):
    await drive_stack.connect()
    drive_stack.google.revoked = True
    assert await drive_stack.sync() == 0  # does not raise; other sources keep syncing

    (source,) = (await drive_stack.client.get("/sources")).json()["sources"]
    assert "Connect Google Drive again" in source["error"]

    drive_stack.google.revoked = False
    await drive_stack.connect()
    (source,) = (await drive_stack.client.get("/sources")).json()["sources"]
    assert source["error"] is None


async def test_disconnect_removes_the_account_and_its_documents(drive_stack):
    drive_stack.google.put("txt-1", "note.txt", "text/plain", b"A short note.")
    await drive_stack.connect()
    await drive_stack.sync()
    await drive_stack.work()
    (source,) = (await drive_stack.client.get("/sources")).json()["sources"]
    assert source["documents"] == 1

    assert (await drive_stack.client.delete(f"/sources/{source['id']}")).status_code == 204
    assert await gdrive_sources() == []
    assert await documents() == {}
    assert (await drive_stack.client.delete(f"/sources/{source['id']}")).status_code == 404
