"""Pack text blocks into overlapping chunks that keep their exact location.

Rules:
- Chunks never cross a heading or page boundary, so each chunk has exactly one
  heading path and one page: "where was this mentioned" stays precise.
- Consecutive chunks in a section overlap by up to `overlap_tokens` (whole blocks).
- A single block longer than `max_tokens` is split into overlapping word windows.
- `char_start`/`char_end` index into the document's full text (blocks joined by
  blank lines), which the UI uses for highlighting.
"""

import math
import re
from dataclasses import dataclass

from app.ingest.extract import Block

BLOCK_SEPARATOR = "\n\n"
TOKENS_PER_WORD = 1.3  # rough average for English/German prose; avoids a tokenizer dependency

_WORD = re.compile(r"\S+")


def estimate_tokens(text: str) -> int:
    return math.ceil(len(_WORD.findall(text)) * TOKENS_PER_WORD)


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    char_start: int
    char_end: int
    page: int | None
    heading_path: str | None


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    tokens: int
    page: int | None
    heading_path: str | None


def full_text(blocks: list[Block]) -> str:
    return BLOCK_SEPARATOR.join(b.text for b in blocks)


def chunk_blocks(blocks: list[Block], max_tokens: int, overlap_tokens: int) -> list[Chunk]:
    if overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be smaller than max_tokens")

    text = full_text(blocks)
    spans: list[tuple[int, int]] = []  # (start, end) of each finished chunk
    meta: list[tuple[int | None, str | None]] = []
    current: list[_Unit] = []

    def emit(start: int, end: int, page: int | None, heading: str | None) -> None:
        spans.append((start, end))
        meta.append((page, heading))

    def flush() -> None:
        if current:
            emit(current[0].start, current[-1].end, current[0].page, current[0].heading_path)

    def overlap_tail() -> list[_Unit]:
        tail: list[_Unit] = []
        total = 0
        for unit in reversed(current):
            if total + unit.tokens > overlap_tokens:
                break
            tail.insert(0, unit)
            total += unit.tokens
        return tail

    offset = 0
    for block in blocks:
        unit = _Unit(
            start=offset,
            end=offset + len(block.text),
            tokens=estimate_tokens(block.text),
            page=block.page,
            heading_path=block.heading_path,
        )
        offset = unit.end + len(BLOCK_SEPARATOR)

        if unit.tokens > max_tokens:
            flush()
            current = []
            for start, end in _split_long(text, unit.start, unit.end, max_tokens, overlap_tokens):
                emit(start, end, unit.page, unit.heading_path)
            continue

        if current and (unit.heading_path, unit.page) != (
            current[0].heading_path,
            current[0].page,
        ):
            flush()
            current = []
        elif current and sum(u.tokens for u in current) + unit.tokens > max_tokens:
            flush()
            current = overlap_tail()
            if sum(u.tokens for u in current) + unit.tokens > max_tokens:
                current = []
        current.append(unit)
    flush()

    return [
        Chunk(i, text[start:end], start, end, page, heading)
        for i, ((start, end), (page, heading)) in enumerate(zip(spans, meta, strict=True))
    ]


def _split_long(
    text: str, start: int, end: int, max_tokens: int, overlap_tokens: int
) -> list[tuple[int, int]]:
    """Overlapping word windows over text[start:end], returned as absolute char spans."""
    words = [(m.start(), m.end()) for m in _WORD.finditer(text, start, end)]  # absolute offsets
    window = max(1, int(max_tokens / TOKENS_PER_WORD))
    step = max(1, window - int(overlap_tokens / TOKENS_PER_WORD))
    spans = []
    for i in range(0, len(words), step):
        piece = words[i : i + window]
        spans.append((piece[0][0], piece[-1][1]))
        if i + window >= len(words):
            break
    return spans
