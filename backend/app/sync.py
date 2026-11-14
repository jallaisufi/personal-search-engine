"""Shared sync logic: work out what changed in a source and index one document.

Used by the scheduler (plans changes and queues jobs), the worker (runs jobs),
and `sync_once` (does both inline, for tests and one-off runs).
"""

import asyncio
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import text

from app.cache import publish_index_version
from app.config import Settings
from app.connectors import local
from app.connectors.gdrive import SUPPORTED, DriveClient, DriveFile, GoogleOAuth
from app.crypto import Cipher
from app.db import read_conn, write_conn
from app.deadlines import extract_deadlines
from app.ingest.embed import EmbeddingProvider
from app.ingest.pipeline import (
    DocumentInput,
    IngestStats,
    bump_index_version,
    ingest_file,
    mark_failed,
)
from app.llm import LLMProvider
from app.queue import Job

log = logging.getLogger(__name__)


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


def job_document(job: Job) -> DocumentInput:
    return DocumentInput(
        source_id=job.source_id,
        external_id=job.external_id,
        title=job.title,
        mime_type=job.mime_type,
        web_url=job.web_url,
        content_hash=job.content_hash,
        modified_at=datetime.fromisoformat(job.modified_at) if job.modified_at else None,
    )


async def plan_local(source_id: int, root: Path) -> tuple[list[Job], list[str]]:
    """Compare the folder with the index. Returns (documents to index, ids to remove).

    Cheap by design: a file whose modification time is unchanged is skipped without
    being read; otherwise its content hash decides whether anything really changed.
    """
    async with read_conn() as conn:
        rows = await conn.execute(
            text(
                "SELECT external_id, content_hash, modified_at, status "
                "FROM documents WHERE source_id = :s"
            ),
            {"s": source_id},
        )
        known = {r.external_id: r for r in rows}

    jobs: list[Job] = []
    seen: set[str] = set()
    files = await asyncio.to_thread(lambda: list(local.scan(root)))  # filesystem walk off the loop
    for file in files:
        seen.add(file.external_id)
        previous = known.get(file.external_id)
        waiting = previous is not None and previous.status == "pending"
        if previous and previous.modified_at == file.modified_at and not waiting:
            continue  # untouched since it was indexed (or failed): no need to read the file

        digest = await asyncio.to_thread(local.content_hash, file.path)
        if previous and previous.content_hash == digest and previous.status == "indexed":
            continue  # touched but identical content

        jobs.append(
            Job(
                kind="local",
                source_id=source_id,
                external_id=file.external_id,
                title=file.title,
                mime_type=file.mime_type,
                content_hash=digest,
                modified_at=file.modified_at.isoformat(),
            )
        )
    return jobs, sorted(set(known) - seen)


async def remove_documents(source_id: int, external_ids: list[str]) -> None:
    if not external_ids:
        return
    async with write_conn() as conn:
        await conn.execute(
            text("DELETE FROM documents WHERE source_id = :s AND external_id = ANY(:ids)"),
            {"s": source_id, "ids": external_ids},
        )
    log.info("removed %d deleted documents", len(external_ids))


# ── Google Drive ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DriveSource:
    id: int
    name: str  # the account's email address
    credentials: bytes
    sync_cursor: str | None


async def gdrive_sources() -> list[DriveSource]:
    async with read_conn() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, name, credentials, sync_cursor FROM sources "
                "WHERE kind = 'gdrive' AND credentials IS NOT NULL ORDER BY id"
            )
        )
        return [DriveSource(r.id, r.name, bytes(r.credentials), r.sync_cursor) for r in rows]


async def record_sync(source_id: int, cursor: str | None, error: str | None) -> None:
    """Save where the next sync starts and whether this one worked (shown in the UI)."""
    async with write_conn() as conn:
        await conn.execute(
            text("""
            UPDATE sources SET
                sync_cursor = COALESCE(CAST(:cursor AS text), sync_cursor),
                last_synced = CASE WHEN CAST(:error AS text) IS NULL THEN now()
                                   ELSE last_synced END,
                config = CASE WHEN CAST(:error AS text) IS NULL THEN config - 'error'
                              ELSE jsonb_set(config, '{error}', to_jsonb(CAST(:error AS text)))
                         END
            WHERE id = :id
            """),
            {"id": source_id, "cursor": cursor, "error": error},
        )


class DriveRegistry:
    """Drive clients per connected account, built from the encrypted credentials."""

    def __init__(self, oauth: GoogleOAuth, cipher: Cipher):
        self.oauth = oauth
        self.cipher = cipher
        self._clients: dict[str, DriveClient] = {}

    def client(self, credentials: bytes) -> DriveClient:
        refresh_token = self.cipher.decrypt(credentials)["refresh_token"]
        if refresh_token not in self._clients:  # keeps the access token between jobs
            self._clients[refresh_token] = DriveClient(self.oauth, refresh_token)
        return self._clients[refresh_token]

    async def for_source(self, source_id: int) -> DriveClient:
        async with read_conn() as conn:
            credentials = (
                await conn.execute(
                    text("SELECT credentials FROM sources WHERE id = :id AND kind = 'gdrive'"),
                    {"id": source_id},
                )
            ).scalar_one_or_none()
        if credentials is None:
            raise LookupError("This Google Drive account is no longer connected")
        return self.client(bytes(credentials))


def make_drives(settings: Settings) -> DriveRegistry | None:
    """The Drive registry for this process, or None when Google Drive isn't configured."""
    if not (settings.google_client_id and settings.google_client_secret):
        return None
    oauth = GoogleOAuth(
        settings.google_client_id, settings.google_client_secret, settings.google_redirect_uri
    )
    return DriveRegistry(oauth, Cipher(settings.encryption_key or ""))


def _drive_job(source_id: int, file: DriveFile) -> Job:
    return Job(
        kind="gdrive",
        source_id=source_id,
        external_id=file.id,
        title=file.name,
        mime_type=file.mime_type,
        content_hash=file.content_hash,
        modified_at=file.modified_time,
        web_url=file.web_url,
    )


async def plan_gdrive(
    source: DriveSource, drive: DriveClient, max_bytes: int
) -> tuple[list[Job], list[str], str]:
    """What to index and remove for one Drive account, and the cursor for the next sync.

    First sync: one full listing. Afterwards only the Changes API is called, so the cost
    of a sync depends on how much changed, not on how big the Drive is.
    """
    async with read_conn() as conn:
        rows = await conn.execute(
            text("""
            SELECT external_id, title, mime_type, web_url, content_hash, modified_at, status
            FROM documents WHERE source_id = :s
            """),
            {"s": source.id},
        )
        known = {r.external_id: r for r in rows}

    files: list[DriveFile] = []
    removed: set[str] = set()
    if source.sync_cursor is None:
        cursor = await drive.start_page_token()  # taken first, so nothing slips between
        files = [file async for file in drive.list_files()]
        removed = set(known) - {file.id for file in files}
    else:
        changes, cursor = await drive.changes(source.sync_cursor)
        for change in changes:
            file = change.file
            if change.removed or file is None or file.trashed or not file.supported:
                removed.add(change.file_id)
            else:
                removed.discard(file.id)
                files = [f for f in files if f.id != file.id] + [file]  # latest change wins

    jobs: dict[str, Job] = {}
    for file in files:
        if not file.supported or (file.size or 0) > max_bytes:
            continue
        previous = known.get(file.id)
        if previous and previous.content_hash == file.content_hash and previous.status != "pending":
            continue  # renamed, moved or just opened: content is unchanged
        jobs[file.id] = _drive_job(source.id, file)

    # Documents still waiting from an earlier sync (their job may have been lost): queue again.
    for external_id, row in known.items():
        if row.status == "pending" and external_id not in jobs and external_id not in removed:
            jobs[external_id] = Job(
                kind="gdrive",
                source_id=source.id,
                external_id=external_id,
                title=row.title,
                mime_type=row.mime_type,
                content_hash=row.content_hash,
                modified_at=row.modified_at.isoformat() if row.modified_at else None,
                web_url=row.web_url,
            )
    return list(jobs.values()), sorted(removed & set(known)), cursor


# ── Indexing one document ────────────────────────────────────────────────────


def _locate(root: Path, external_id: str) -> Path | None:
    """The file for a job, or None if it no longer exists. Never leaves the documents folder."""
    path = (root / external_id).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError(f"{external_id} is outside the documents folder")
    return path if path.is_file() else None


async def index_job(
    job: Job,
    root: Path,
    provider: EmbeddingProvider,
    settings: Settings,
    drives: DriveRegistry | None = None,
) -> IngestStats | None:
    """Index one document. Returns None when it no longer exists. Raises on failure."""
    if job.kind == "gdrive":
        if drives is None:
            raise RuntimeError("Google Drive isn't configured on this worker")
        drive = await drives.for_source(job.source_id)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / f"document{SUPPORTED.get(job.mime_type or '', '.txt')}"
            if not await drive.download(job.external_id, job.mime_type or "", path):
                return None  # deleted in Drive since it was queued
            stats = await ingest_file(job_document(job), path, provider, settings)
    else:
        path = await asyncio.to_thread(_locate, root, job.external_id)
        if path is None:
            return None  # deleted since it was queued; the scheduler removes it from the index
        stats = await ingest_file(job_document(job), path, provider, settings)

    log.info(
        "indexed %s: %d chunks (%d embedded, %d reused) in %.2fs",
        job.title,
        stats.chunks,
        stats.embedded,
        stats.reused,
        stats.seconds,
    )
    return stats


async def publish_changes(redis: Redis | None) -> int:
    """Bump the index version so every cached search result is retired."""
    version = await bump_index_version()
    if redis is not None:
        await publish_index_version(redis, version)
    return version


async def sync_once(
    source_id: int,
    root: Path,
    provider: EmbeddingProvider,
    settings: Settings,
    redis: Redis | None = None,
    llm: LLMProvider | None = None,
) -> int:
    """Plan and index inline, without the queue. Returns the number of documents changed."""
    jobs, removed = await plan_local(source_id, root)
    for job in jobs:
        try:
            if await index_job(job, root, provider, settings) and llm is not None:
                await extract_deadlines(job.source_id, job.external_id, llm, redis)
        except Exception as exc:  # one bad file must not stop the sync
            log.exception("failed to index %s", job.external_id)
            await mark_failed(job_document(job), f"{type(exc).__name__}: {exc}")
    await remove_documents(source_id, removed)

    changed = len(jobs) + len(removed)
    if changed:
        version = await publish_changes(redis)
        log.info("sync done: %d changes, index version %d", changed, version)
    return changed
