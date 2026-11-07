"""Embedding providers behind one interface, chosen by EMBEDDING_PROVIDER.

Swapping Ollama for OpenAI (or the deterministic fake used in CI) needs no code changes.
"""

import asyncio
import hashlib
import logging
import math
import re
from typing import Protocol

import httpx

from app.config import Settings

log = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...
    async def ready(self) -> bool: ...
    async def close(self) -> None: ...


class _HttpProvider:
    """Shared batching and retry logic for HTTP-based providers."""

    def __init__(self, model: str, dim: int, batch_size: int, client: httpx.AsyncClient):
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.client = client

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def _doc_prefix(self) -> str:
        return ""

    def _query_prefix(self) -> str:
        return ""

    async def _with_retry(self, texts: list[str]) -> list[list[float]]:
        delay = 1.0
        for attempt in range(1, 5):
            try:
                vectors = await self._embed_batch(texts)
                if vectors and len(vectors[0]) != self.dim:
                    raise ValueError(
                        f"Model '{self.model}' returned {len(vectors[0])} dims, "
                        f"but EMBEDDING_DIM={self.dim}"
                    )
                return vectors
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == 4:
                    raise
                log.warning("embedding call failed (attempt %d): %s", attempt, exc)
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        prefix = self._doc_prefix()
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [prefix + t for t in texts[i : i + self.batch_size]]
            out.extend(await self._with_retry(batch))
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self._with_retry([self._query_prefix() + text]))[0]

    async def ready(self) -> bool:
        try:
            await self._embed_batch(["ping"])
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
            log.info("embedding provider not ready: %s", exc)
            return False

    async def close(self) -> None:
        await self.client.aclose()


class OllamaProvider(_HttpProvider):
    def __init__(self, settings: Settings):
        super().__init__(
            settings.embedding_model,
            settings.embedding_dim,
            settings.embedding_batch_size,
            httpx.AsyncClient(base_url=settings.ollama_url, timeout=120),
        )

    # nomic-embed-text is trained with task prefixes; using them improves retrieval.
    def _doc_prefix(self) -> str:
        return "search_document: " if "nomic" in self.model else ""

    def _query_prefix(self) -> str:
        return "search_query: " if "nomic" in self.model else ""

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.post("/api/embed", json={"model": self.model, "input": texts})
        resp.raise_for_status()
        return resp.json()["embeddings"]


class OpenAIProvider(_HttpProvider):
    def __init__(self, settings: Settings):
        super().__init__(
            settings.embedding_model,
            settings.embedding_dim,
            settings.embedding_batch_size,
            httpx.AsyncClient(
                base_url="https://api.openai.com/v1",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                timeout=60,
            ),
        )

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        resp = await self.client.post(
            "/embeddings",
            json={"model": self.model, "input": texts, "dimensions": self.dim},
        )
        resp.raise_for_status()
        return [item["embedding"] for item in sorted(resp.json()["data"], key=lambda d: d["index"])]


class FakeProvider:
    """Deterministic hashed bag-of-words vectors: no model, no network, no cost.

    Texts sharing words get similar vectors, which is enough to test the whole
    pipeline (including ranking) in CI.
    """

    _token = re.compile(r"\w+")

    def __init__(self, dim: int):
        self.model = "fake-hash"
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in self._token.findall(text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            vec[int.from_bytes(digest[:4], "little") % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        if norm == 1.0 and not any(vec):
            vec[0] = 1.0  # pgvector cosine distance is undefined for zero vectors
        return [v / norm for v in vec]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def make_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "ollama":
        return OllamaProvider(settings)
    if settings.embedding_provider == "openai":
        return OpenAIProvider(settings)
    return FakeProvider(settings.embedding_dim)


def to_pgvector(vec: list[float]) -> str:
    """pgvector text literal; passed as a parameter and CAST in SQL."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
