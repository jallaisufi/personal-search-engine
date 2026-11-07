import pytest

from app.ingest.chunk import chunk_blocks, estimate_tokens, full_text, stitch_text
from app.ingest.extract import Block


def words(n: int, word: str = "lorem") -> str:
    return " ".join([word] * n)


def test_small_document_is_one_chunk():
    blocks = [
        Block("Hello world.", heading_path="Intro"),
        Block("Second paragraph.", heading_path="Intro"),
    ]
    chunks = chunk_blocks(blocks, max_tokens=100, overlap_tokens=10)
    assert len(chunks) == 1
    assert chunks[0].text == "Hello world.\n\nSecond paragraph."
    assert chunks[0].heading_path == "Intro"


def test_offsets_point_into_full_text():
    blocks = [Block(words(30, f"w{i}"), heading_path="A") for i in range(10)]
    text = full_text(blocks)
    chunks = chunk_blocks(blocks, max_tokens=100, overlap_tokens=40)
    assert len(chunks) > 1
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text
        assert estimate_tokens(c.text) <= 100


def test_chunks_never_cross_heading_boundaries():
    blocks = [
        Block("Rent is due monthly.", heading_path="Lease > Rent"),
        Block("Deposit is three months.", heading_path="Lease > Deposit"),
    ]
    chunks = chunk_blocks(blocks, max_tokens=500, overlap_tokens=50)
    assert [c.heading_path for c in chunks] == ["Lease > Rent", "Lease > Deposit"]


def test_consecutive_chunks_overlap_within_a_section():
    blocks = [Block(words(20, f"p{i}"), heading_path="S") for i in range(6)]
    chunks = chunk_blocks(blocks, max_tokens=60, overlap_tokens=30)
    assert len(chunks) >= 2
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.char_start < prev.char_end  # shared block


def test_long_block_is_split_into_overlapping_windows():
    blocks = [Block(" ".join(f"t{i}" for i in range(1000)), page=3)]
    chunks = chunk_blocks(blocks, max_tokens=130, overlap_tokens=26)
    assert len(chunks) > 5
    assert all(c.page == 3 for c in chunks)
    assert all(estimate_tokens(c.text) <= 130 for c in chunks)
    assert chunks[0].text.startswith("t0 ")
    assert chunks[-1].text.endswith("t999")
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.char_start < prev.char_end


def test_chunks_never_cross_pages():
    blocks = [Block("Page one text.", page=1), Block("Page two text.", page=2)]
    chunks = chunk_blocks(blocks, max_tokens=500, overlap_tokens=10)
    assert [(c.text, c.page) for c in chunks] == [("Page one text.", 1), ("Page two text.", 2)]


def test_overlap_must_be_smaller_than_chunk():
    with pytest.raises(ValueError):
        chunk_blocks([Block("x")], max_tokens=10, overlap_tokens=10)


def test_empty_document_has_no_chunks():
    assert chunk_blocks([], max_tokens=100, overlap_tokens=10) == []


def test_stitch_text_rebuilds_the_document_exactly():
    blocks = [Block(words(25, f"b{i}"), heading_path="S" if i < 4 else "T") for i in range(8)]
    blocks.append(Block(" ".join(f"long{i}" for i in range(400))))
    original = full_text(blocks)
    chunks = chunk_blocks(blocks, max_tokens=80, overlap_tokens=35)
    rebuilt = stitch_text([(c.char_start, c.char_end, c.text) for c in reversed(chunks)])
    assert rebuilt == original
