import math

from app.ingest.embed import FakeProvider, to_pgvector


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # vectors are normalised


async def test_fake_provider_is_deterministic_and_normalised():
    p = FakeProvider(dim=64)
    a1, a2 = await p.embed_documents(["security deposit refund", "security deposit refund"])
    assert a1 == a2
    assert math.isclose(sum(x * x for x in a1), 1.0, rel_tol=1e-9)


async def test_fake_provider_similarity_follows_shared_words():
    p = FakeProvider(dim=256)
    q = await p.embed_query("security deposit")
    near, far = await p.embed_documents(
        ["The security deposit is three months of rent.", "Train departs at 07:28."]
    )
    assert cosine(q, near) > cosine(q, far)


async def test_fake_provider_handles_empty_text():
    (vec,) = await FakeProvider(dim=8).embed_documents([""])
    assert any(vec)  # pgvector cosine distance needs a non-zero vector


def test_pgvector_literal():
    assert to_pgvector([0.5, -1.0, 0.0]) == "[0.5,-1,0]"
