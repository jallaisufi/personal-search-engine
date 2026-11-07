"""Turn files into text blocks that remember where they came from (page, heading path).

Kept free of database and network code so it can be unit-tested and run in a thread.
"""

import re
from dataclasses import dataclass
from pathlib import Path

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".markdown", ".txt"}

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_DOCX_HEADING = re.compile(r"^Heading (\d)$")


@dataclass(frozen=True)
class Block:
    """A paragraph-sized piece of text plus its location in the source document."""

    text: str
    page: int | None = None
    heading_path: str | None = None


class _HeadingStack:
    def __init__(self) -> None:
        self._levels: list[tuple[int, str]] = []

    def push(self, level: int, title: str) -> None:
        while self._levels and self._levels[-1][0] >= level:
            self._levels.pop()
        self._levels.append((level, title.strip()))

    @property
    def path(self) -> str | None:
        return " > ".join(t for _, t in self._levels) or None


def extract(path: Path) -> list[Block]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix in {".md", ".markdown", ".txt"}:
        return extract_markdown(path.read_text(encoding="utf-8", errors="replace"))
    raise ValueError(f"Unsupported file type: {suffix}")


def extract_markdown(text: str) -> list[Block]:
    """Paragraphs split on blank lines; '#' headings build the heading path."""
    blocks: list[Block] = []
    headings = _HeadingStack()
    para: list[str] = []

    def flush() -> None:
        if para:
            joined = "\n".join(para).strip()
            if joined:
                blocks.append(Block(joined, heading_path=headings.path))
            para.clear()

    for line in text.splitlines():
        match = _MD_HEADING.match(line)
        if match:
            flush()
            headings.push(len(match.group(1)), match.group(2))
            blocks.append(Block(match.group(2).strip(), heading_path=headings.path))
        elif not line.strip():
            flush()
        else:
            para.append(line.rstrip())
    flush()
    return blocks


def extract_docx(path: Path) -> list[Block]:
    import docx  # imported lazily: only workers need it

    blocks: list[Block] = []
    headings = _HeadingStack()
    for paragraph in docx.Document(str(path)).paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        match = _DOCX_HEADING.match(paragraph.style.name if paragraph.style else "")
        if match:
            headings.push(int(match.group(1)), text)
        blocks.append(Block(text, heading_path=headings.path))
    return blocks


def extract_pdf(path: Path) -> list[Block]:
    import fitz  # PyMuPDF, imported lazily

    blocks: list[Block] = []
    with fitz.open(str(path)) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            # Each text block is roughly a paragraph: (x0, y0, x1, y1, text, block_no, type)
            for item in page.get_text("blocks", sort=True):
                if item[6] != 0:  # skip image blocks
                    continue
                text = " ".join(item[4].split())
                if text:
                    blocks.append(Block(text, page=page_number))
    return blocks
