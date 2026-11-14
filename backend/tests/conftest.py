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
    """Skip tests whose service isn't available: Postgres (DATABASE_URL), Redis (REDIS_URL)."""
    needs = {"integration": "DATABASE_URL", "redis": "REDIS_URL"}
    for item in items:
        for marker, variable in needs.items():
            if marker in item.keywords and not os.environ.get(variable):
                item.add_marker(pytest.mark.skip(reason=f"set {variable} to run this test"))
