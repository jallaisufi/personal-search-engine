"""Initial schema: sources, documents, chunks (pgvector + full-text), deadlines, index_state.

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

from alembic import op

from app.config import get_settings

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    dim = get_settings().embedding_dim

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE sources (
            id           BIGSERIAL PRIMARY KEY,
            kind         TEXT NOT NULL CHECK (kind IN ('local', 'gdrive')),
            name         TEXT NOT NULL,
            config       JSONB NOT NULL DEFAULT '{}',
            credentials  BYTEA,
            sync_cursor  TEXT,
            last_synced  TIMESTAMPTZ,
            UNIQUE (kind, name)
        )
    """)

    op.execute("""
        CREATE TABLE documents (
            id            BIGSERIAL PRIMARY KEY,
            source_id     BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            external_id   TEXT NOT NULL,
            title         TEXT NOT NULL,
            mime_type     TEXT,
            web_url       TEXT,
            content_hash  TEXT NOT NULL,
            modified_at   TIMESTAMPTZ,
            indexed_at    TIMESTAMPTZ,
            status        TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'indexed', 'failed')),
            error         TEXT,
            UNIQUE (source_id, external_id)
        )
    """)

    op.execute(f"""
        CREATE TABLE chunks (
            id            BIGSERIAL PRIMARY KEY,
            document_id   BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            ordinal       INT NOT NULL,
            page          INT,
            heading_path  TEXT,
            char_start    INT NOT NULL,
            char_end      INT NOT NULL,
            text          TEXT NOT NULL,
            text_hash     TEXT NOT NULL,
            embedding     vector({dim}) NOT NULL,
            tsv           tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED
        )
    """)
    # HNSW: approximate nearest neighbour without a training step; handles inserts well.
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute("CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv)")
    op.execute("CREATE INDEX chunks_text_hash ON chunks (text_hash)")
    op.execute("CREATE INDEX chunks_document_id ON chunks (document_id)")

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

    # Bumped after each ingest batch; part of every search cache key (weekend 2).
    op.execute(f"""
        CREATE TABLE index_state (
            id               INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            version          BIGINT NOT NULL DEFAULT 0,
            embedding_model  TEXT,
            embedding_dim    INT NOT NULL DEFAULT {dim}
        )
    """)
    op.execute("INSERT INTO index_state (id) VALUES (1)")


def downgrade() -> None:
    for table in ("index_state", "deadlines", "chunks", "documents", "sources"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
