"""Ask mode: retrieval-augmented answers with numbered citations.

The model only sees the retrieved passages and is told to answer from them alone,
cite them as [n], and say so when they don't contain the answer.
"""

import re
from typing import Any

SYSTEM_PROMPT = """\
You answer questions about the user's own documents.

Rules:
- Use only the numbered sources provided. Do not use outside knowledge.
- After every sentence that uses a source, cite it like [1] or [2][3].
- If the sources don't contain the answer, say "I couldn't find this in your documents." \
and nothing else.
- Be concise: a short paragraph, or a short list when listing several items.
- Answer in the language of the question."""

_CITATION = re.compile(r"\[(\d{1,2})\]")


def build_user_prompt(question: str, sources: list[dict[str, Any]]) -> str:
    parts = []
    for i, source in enumerate(sources, start=1):
        where = [source["title"]]
        if source.get("heading_path"):
            where.append(source["heading_path"])
        if source.get("page"):
            where.append(f"page {source['page']}")
        parts.append(f"[{i}] {' / '.join(where)}\n{source['text'].strip()}")
    return "Sources:\n\n" + "\n\n".join(parts) + f"\n\nQuestion: {question}"


def cited_numbers(answer: str, source_count: int) -> list[int]:
    """Source numbers the answer actually cites, in order of first use (invalid ones dropped)."""
    seen: list[int] = []
    for match in _CITATION.finditer(answer):
        n = int(match.group(1))
        if 1 <= n <= source_count and n not in seen:
            seen.append(n)
    return seen
