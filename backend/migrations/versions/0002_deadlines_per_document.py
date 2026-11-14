"""Deadlines belong to a document and survive re-indexing when dismissed.

The table from 0001 was never written to. The new shape links a deadline to its
document (not only to a chunk, which is replaced on every re-index), stores the
sentence it came from with its exact position, and keeps dismissals.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-14
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deadlines")
    op.execute("""
        CREATE TABLE deadlines (
            id           BIGSERIAL PRIMARY KEY,
            document_id  BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            chunk_id     BIGINT REFERENCES chunks(id) ON DELETE SET NULL,
            task         TEXT NOT NULL,
            due_on       DATE NOT NULL,
            quote        TEXT NOT NULL,
            char_start   INT NOT NULL,
            char_end     INT NOT NULL,
            dismissed    BOOLEAN NOT NULL DEFAULT false,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX deadlines_upcoming ON deadlines (due_on) WHERE NOT dismissed")
    op.execute("CREATE INDEX deadlines_document ON deadlines (document_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS deadlines")
    op.execute("""
        CREATE TABLE deadlines (
            id          BIGSERIAL PRIMARY KEY,
            chunk_id    BIGINT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
            task        TEXT NOT NULL,
            due_at      TIMESTAMPTZ NOT NULL,
            confidence  REAL,
            dismissed   BOOLEAN NOT NULL DEFAULT false
        )
    """)
    op.execute("CREATE INDEX deadlines_due ON deadlines (due_at) WHERE NOT dismissed")
