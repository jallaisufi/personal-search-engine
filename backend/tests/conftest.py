import os
from pathlib import Path

import pytest

# Tests never call a real model: deterministic fake embeddings, small chunks.
os.environ["EMBEDDING_PROVIDER"] = "fake"
os.environ["LLM_PROVIDER"] = "fake"
os.environ.setdefault("EMBEDDING_DIM", "768")
os.environ.setdefault("CHUNK_TOKENS", "120")
os.environ.setdefault("CHUNK_OVERLAP_TOKENS", "20")

SAMPLE_DOCS = Path(__file__).resolve().parents[2] / "sample-docs"


@pytest.fixture(scope="session")
def sample_docs() -> Path:
    return SAMPLE_DOCS


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Integration tests need Postgres; skip them unless DATABASE_URL is set."""
    if os.environ.get("DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="set DATABASE_URL to run integration tests")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
