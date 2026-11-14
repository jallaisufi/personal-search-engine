"""Deadlines found in the user's documents."""

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from app.db import read_conn, write_conn

router = APIRouter(prefix="/deadlines")


class DeadlineOut(BaseModel):
    id: int
    task: str
    due_on: date
    days_left: int  # negative when the date has passed
    quote: str  # the sentence the deadline comes from
    # Where it is, in the shape the document preview needs:
    document_id: int
    title: str
    path: str
    web_url: str | None
    chunk_id: int
    page: int | None
    heading_path: str | None
    char_start: int
    char_end: int
    doc_length: int


@router.get("", response_model=list[DeadlineOut])
async def list_deadlines(include_past: bool = False) -> list[DeadlineOut]:
    """Upcoming deadlines, soonest first. `include_past=true` also returns passed ones."""
    async with read_conn() as conn:
        rows = await conn.execute(
            text("""
            SELECT dl.id, dl.task, dl.due_on, dl.due_on - CURRENT_DATE AS days_left, dl.quote,
                   dl.char_start, dl.char_end,
                   d.id AS document_id, d.title, d.web_url,
                   CASE WHEN s.kind = 'gdrive' THEN 'Google Drive (' || s.name || ')'
                        ELSE d.external_id END AS path,
                   c.id AS chunk_id, c.page, c.heading_path,
                   (SELECT max(c2.char_end) FROM chunks c2 WHERE c2.document_id = d.id)
                       AS doc_length
            FROM deadlines dl
            JOIN documents d ON d.id = dl.document_id
            JOIN sources s ON s.id = d.source_id
            JOIN chunks c ON c.id = dl.chunk_id
            WHERE NOT dl.dismissed
              AND (CAST(:include_past AS boolean) OR dl.due_on >= CURRENT_DATE)
            ORDER BY dl.due_on, dl.id
            LIMIT 500
            """),
            {"include_past": include_past},
        )
        return [DeadlineOut(**r._mapping) for r in rows]


@router.post("/{deadline_id}/dismiss", status_code=204)
async def dismiss(deadline_id: int) -> None:
    """Hide a deadline that is done or was misread. It stays hidden after re-indexing."""
    async with write_conn() as conn:
        updated = await conn.execute(
            text("UPDATE deadlines SET dismissed = true WHERE id = :id RETURNING id"),
            {"id": deadline_id},
        )
        if updated.first() is None:
            raise HTTPException(status_code=404, detail="No such deadline")
