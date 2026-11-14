"""Ingestion job queue on Redis Streams.

The scheduler adds one job per new or changed document; any number of workers
consume them through a consumer group, so each job goes to exactly one worker.

    scheduler ──XADD──▶ stream ──XREADGROUP──▶ worker ──ok──▶ XACK
                          ▲                      │ failed
                          │ due                  ▼
                       delayed (sorted set, exponential backoff)
                                                 │ attempts used up
                                                 ▼
                                             dead letters

Guarantees: at-least-once delivery. A job stays "pending" in Redis until a worker
acknowledges it, so a worker that dies mid-job doesn't lose it: another worker
reclaims it after `claim_idle_ms`. Indexing a document is idempotent (it replaces
the document's chunks in one transaction), so a rare double run is harmless.
"""

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

log = logging.getLogger(__name__)

GROUP = "workers"
DEAD_LETTER_LIMIT = 1000


@dataclass(frozen=True)
class Job:
    kind: str  # which connector the document comes from, e.g. "local"
    source_id: int
    external_id: str
    title: str
    mime_type: str | None
    content_hash: str
    modified_at: str | None  # ISO 8601
    attempt: int = 0
    web_url: str | None = None  # link to open the original (Drive)
    task: str = "index"  # "index" the document, or extract its "deadlines" afterwards

    def dumps(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def loads(cls, raw: str | bytes) -> "Job":
        return cls(**json.loads(raw))


@dataclass(frozen=True)
class Message:
    id: str
    job: Job


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class IngestQueue:
    def __init__(
        self,
        redis: Redis,
        consumer: str,
        *,
        prefix: str = "ingest",
        max_attempts: int = 3,
        retry_base_seconds: float = 30,
        claim_idle_ms: int = 15 * 60 * 1000,
        dedup_ttl_seconds: int = 3600,
    ):
        self.redis = redis
        self.consumer = consumer
        self.stream = f"{prefix}:jobs"
        self.delayed = f"{prefix}:delayed"
        self.dead = f"{prefix}:dead"
        self.prefix = prefix
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.claim_idle_ms = claim_idle_ms
        self.dedup_ttl_seconds = dedup_ttl_seconds

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):  # already exists: fine
                raise

    # ── producing ────────────────────────────────────────────────────────
    def _dedup_key(self, job: Job) -> str:
        doc = hashlib.sha256(f"{job.source_id}:{job.external_id}".encode()).hexdigest()[:24]
        return f"{self.prefix}:queued:{job.task}:{doc}:{job.content_hash[:16]}"

    async def enqueue(self, job: Job) -> bool:
        """Add a job unless the same document version is already queued or in flight."""
        if not await self.redis.set(self._dedup_key(job), 1, nx=True, ex=self.dedup_ttl_seconds):
            return False
        await self.redis.xadd(self.stream, {"job": job.dumps()})
        return True

    # ── consuming ────────────────────────────────────────────────────────
    async def read(self, count: int = 8, block_ms: int = 2000) -> list[Message]:
        """Next jobs for this consumer; waits up to `block_ms` when the queue is empty."""
        try:
            await self._promote_due()
            await self._recover_abandoned(count)
            response = await self.redis.xreadgroup(
                GROUP, self.consumer, {self.stream: ">"}, count=count, block=block_ms
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            await self.ensure_group()  # Redis was flushed or replaced: recreate and carry on
            return []
        messages: list[Message] = []
        for _stream, entries in response or []:
            messages.extend(self._parse(entries))
        return messages

    def _parse(self, entries: list[tuple[Any, dict | None]]) -> list[Message]:
        messages = []
        for message_id, fields in entries:
            if not fields:
                continue  # entry was deleted
            raw = fields.get(b"job") or fields.get("job")
            messages.append(Message(_text(message_id), Job.loads(raw)))
        return messages

    async def done(self, message: Message) -> None:
        await self._ack(message)
        await self.redis.delete(self._dedup_key(message.job))

    async def failed(self, message: Message, error: str, max_attempts: int | None = None) -> bool:
        """Schedule a retry with exponential backoff. Returns False once attempts are used up
        (the job then goes to the dead-letter stream and the caller marks the document failed)."""
        job = replace(message.job, attempt=message.job.attempt + 1)
        if job.attempt >= (max_attempts or self.max_attempts):
            await self.redis.xadd(
                self.dead,
                {"job": job.dumps(), "error": error[:500], "failed_at": str(int(time.time()))},
                maxlen=DEAD_LETTER_LIMIT,
            )
            await self.done(message)
            return False

        delay = self.retry_base_seconds * 4 ** (job.attempt - 1)  # 30 s, 2 min, 8 min, ...
        await self.redis.zadd(self.delayed, {job.dumps(): time.time() + delay})
        await self.redis.expire(
            self._dedup_key(job), int(delay) + self.dedup_ttl_seconds
        )  # keep the scheduler from re-adding it while it waits
        await self._ack(message)
        return True

    async def _ack(self, message: Message) -> None:
        await self.redis.xack(self.stream, GROUP, message.id)
        await self.redis.xdel(self.stream, message.id)

    async def _promote_due(self) -> None:
        """Move retries whose backoff has passed back onto the stream."""
        due = await self.redis.zrangebyscore(self.delayed, 0, time.time(), start=0, num=50)
        for raw in due:
            if await self.redis.zrem(self.delayed, raw):  # only one worker wins each job
                await self.redis.xadd(self.stream, {"job": _text(raw)})

    async def _recover_abandoned(self, count: int) -> None:
        """Jobs delivered to a worker that never acknowledged them (it crashed or was killed).

        They are routed through the failure path, so a job that keeps killing workers
        uses up its attempts and ends in the dead-letter stream instead of looping forever.
        """
        response = await self.redis.xautoclaim(
            self.stream, GROUP, self.consumer, min_idle_time=self.claim_idle_ms, count=count
        )
        for message in self._parse(response[1]):
            log.warning("recovering abandoned job for %s", message.job.external_id)
            await self.failed(message, "worker stopped while processing this document")

    # ── observing ────────────────────────────────────────────────────────
    async def dead_letters(self, count: int = 20) -> list[dict[str, str]]:
        entries = await self.redis.xrevrange(self.dead, count=count)
        return [{_text(k): _text(v) for k, v in fields.items()} for _id, fields in entries]


async def queue_depth(redis: Redis, prefix: str = "ingest") -> dict[str, int]:
    """Jobs waiting or in flight, waiting for a retry, and given up on."""
    try:
        waiting = await redis.xlen(f"{prefix}:jobs")
        retrying = await redis.zcard(f"{prefix}:delayed")
        dead = await redis.xlen(f"{prefix}:dead")
    except Exception as exc:  # noqa: BLE001 - stats must not fail because Redis is down
        log.warning("could not read queue depth: %s", exc)
        return {"waiting": 0, "retrying": 0, "dead": 0}
    return {"waiting": int(waiting), "retrying": int(retrying), "dead": int(dead)}
