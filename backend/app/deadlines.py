"""Stage 2 of deadline extraction: ask the model about candidate passages and store the result.

Runs as its own queue job after a document is indexed, so a slow or unavailable
model never delays search, and extraction is retried on its own.
"""

import logging
from dataclasses import dataclass
from datetime import date

from redis.asyncio import Redis
from sqlalchemy import text

from app.db import primary_engine, write_conn
from app.deadline_rules import SYSTEM_PROMPT, build_prompt, find_candidates, parse_answer
from app.llm import LLMProvider

log = logging.getLogger(__name__)

SCANNED_KEY = "stats:deadlines:passages_scanned"
SENT_KEY = "stats:deadlines:passages_sent_to_model"


@dataclass(frozen=True)
class Found:
    chunk_id: int
    task: str
    due_on: date
    quote: str
    char_start: int  # position of the quote in the document
    char_end: int


@dataclass(frozen=True)
class ExtractionStats:
    passages: int
    sent_to_model: int
    deadlines: int


async def _count(redis: Redis | None, scanned: int, sent: int) -> None:
    if redis is None:
        return
    try:
        await redis.incrby(SCANNED_KEY, scanned)
        await redis.incrby(SENT_KEY, sent)
    except Exception as exc:  # noqa: BLE001 - statistics only
        log.warning("could not record deadline statistics: %s", exc)


async def extract_deadlines(
    source_id: int, external_id: str, llm: LLMProvider, redis: Redis | None = None
) -> ExtractionStats | None:
    """Find and store the deadlines of one document. Returns None if it no longer exists."""
    # Read from the primary: the chunks were written moments ago.
    async with primary_engine().connect() as conn:
        document = (
            await conn.execute(
                text(
                    "SELECT id, modified_at FROM documents "
                    "WHERE source_id = :s AND external_id = :e AND status = 'indexed'"
                ),
                {"s": source_id, "e": external_id},
            )
        ).one_or_none()
        if document is None:
            return None
        chunks = (
            await conn.execute(
                text(
                    "SELECT id, char_start, text FROM chunks "
                    "WHERE document_id = :d ORDER BY ordinal"
                ),
                {"d": document.id},
            )
        ).all()

    # Dates without a year are read relative to when the document was last changed.
    reference = document.modified_at.date() if document.modified_at else date.today()

    found: list[Found] = []
    seen: set[tuple[date, str]] = set()  # overlapping chunks repeat sentences
    sent = 0
    for chunk in chunks:
        candidates = [
            c for c in find_candidates(chunk.text, reference) if (c.day, c.quote) not in seen
        ]
        if not candidates:
            continue  # most passages stop here and never reach the model
        sent += 1
        answer = "".join(
            [
                piece
                async for piece in llm.stream(SYSTEM_PROMPT, build_prompt(chunk.text, candidates))
            ]
        )
        tasks = parse_answer(answer, candidates)
        for candidate in candidates:
            seen.add((candidate.day, candidate.quote))
            if candidate.day in tasks:
                found.append(
                    Found(
                        chunk_id=chunk.id,
                        task=tasks[candidate.day],
                        due_on=candidate.day,
                        quote=candidate.quote,
                        char_start=chunk.char_start + candidate.start,
                        char_end=chunk.char_start + candidate.end,
                    )
                )

    async with write_conn() as conn:
        # Dismissed deadlines are kept, so they don't come back after a re-index.
        await conn.execute(
            text("DELETE FROM deadlines WHERE document_id = :d AND NOT dismissed"),
            {"d": document.id},
        )
        for item in found:
            await conn.execute(
                text("""
                INSERT INTO deadlines (document_id, chunk_id, task, due_on, quote,
                                       char_start, char_end)
                SELECT CAST(:document_id AS bigint), CAST(:chunk_id AS bigint),
                       CAST(:task AS text), CAST(:due_on AS date), CAST(:quote AS text),
                       CAST(:char_start AS int), CAST(:char_end AS int)
                WHERE EXISTS (SELECT 1 FROM chunks WHERE id = CAST(:chunk_id AS bigint))
                  AND NOT EXISTS (
                      SELECT 1 FROM deadlines
                      WHERE document_id = CAST(:document_id AS bigint) AND dismissed
                        AND due_on = CAST(:due_on AS date) AND quote = CAST(:quote AS text))
                """),
                {"document_id": document.id, **item.__dict__},
            )

    await _count(redis, len(chunks), sent)
    stats = ExtractionStats(len(chunks), sent, len(found))
    log.info(
        "deadlines for %s: %d found; %d of %d passages needed the model",
        external_id,
        stats.deadlines,
        stats.sent_to_model,
        stats.passages,
    )
    return stats


async def extraction_stats(redis: Redis) -> dict[str, float | int]:
    try:
        scanned, sent = await redis.mget(SCANNED_KEY, SENT_KEY)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read deadline statistics: %s", exc)
        scanned = sent = None
    scanned, sent = int(scanned or 0), int(sent or 0)
    return {
        "passages_scanned": scanned,
        "passages_sent_to_model": sent,
        "share_sent_to_model": round(sent / scanned, 3) if scanned else 0.0,
    }
