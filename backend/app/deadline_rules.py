"""Finding deadline candidates in text, without a language model.

Stage 1 of deadline extraction. It does two things the model is bad or wasteful at:

- **Parsing dates** deterministically (English and German, with or without a year),
  so a date shown to the user is never invented by a model.
- **Filtering**: only sentences that contain a date *and* a cue word such as "due",
  "by" or "Frist" are candidates. Everything else never reaches the model.

Stage 2 (app/deadlines.py) asks the model one question per candidate passage: which of
these dates are real deadlines, and what has to be done?
"""

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

_MONTHS = {
    1: "january januar jan",
    2: "february februar feb",
    3: "march märz maerz mar",
    4: "april apr",
    5: "may mai",
    6: "june juni jun",
    7: "july juli jul",
    8: "august aug",
    9: "september sept sep",
    10: "october oktober oct okt",
    11: "november nov",
    12: "december dezember dec dez",
}
_MONTH_NUMBER = {name: number for number, names in _MONTHS.items() for name in names.split()}
_MONTH = "(" + "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True)) + r")\.?"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("dmy_numeric", re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b")),
    ("month_end", re.compile(rf"\b(?:end of|ende)\s+{_MONTH}\s+(\d{{4}})\b", re.IGNORECASE)),
    (  # 20 November 2026 · 20th November · 31. Juli 2027
        "day_month",
        re.compile(
            rf"\b(\d{{1,2}})(?:st|nd|rd|th|\.)?\s+{_MONTH}(?:,?\s+(\d{{4}}))?(?![\w])",
            re.IGNORECASE,
        ),
    ),
    (  # November 20, 2026 · November 20th
        "month_day",
        re.compile(
            rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{{4}}))?(?![\w])",
            re.IGNORECASE,
        ),
    ),
]

# A date only counts as a candidate when its sentence also contains one of these.
_CUES = re.compile(
    r"\b(due|deadline|by|until|till|before|no later than|submit|submitted|submission|"
    r"expires?|expiry|ends?|must|renew|pay|"
    r"frist|bis|spätestens|fällig|abgabe|einreichen|ablauf|kündigen|zahlen)\b",
    re.IGNORECASE,
)
# Sentence ends: a line break, or .!? followed by a space. A dot after a number doesn't end
# the sentence when a month name follows, so "31. Juli 2027" stays in one piece.
_MONTH_NAMES = "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True))
_BOUNDARY = re.compile(
    rf"\n+|[!?]\s+|(?<!\d)\.\s+|(?<=\d)\.\s+(?!(?:{_MONTH_NAMES})\b)", re.IGNORECASE
)

MAX_QUOTE_CHARS = 300


@dataclass(frozen=True)
class DateMention:
    day: date
    start: int
    end: int


@dataclass(frozen=True)
class Candidate:
    day: date
    quote: str  # the sentence the date appears in
    start: int  # offsets of the quote within the passage
    end: int


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:  # 31 February, month 13, ...
        return None


def _resolve_year(month: int, day: int, reference: date) -> date | None:
    """A date written without a year means its next occurrence (with a month of slack)."""
    this_year = _make_date(reference.year, month, day)
    if this_year and this_year >= reference - timedelta(days=30):
        return this_year
    return _make_date(reference.year + 1, month, day)


def find_dates(text: str, reference: date) -> list[DateMention]:
    """Every date written in `text`, in order. `reference` resolves dates without a year."""
    found: list[DateMention] = []
    taken: list[tuple[int, int]] = []

    for kind, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue  # already matched by an earlier, more specific pattern
            groups = match.groups()
            day: date | None
            if kind == "iso":
                day = _make_date(int(groups[0]), int(groups[1]), int(groups[2]))
            elif kind == "dmy_numeric":
                d, m = int(groups[0]), int(groups[1])
                if m > 12 >= d:  # written month-first
                    d, m = m, d
                day = _make_date(int(groups[2]), m, d)
            elif kind == "month_end":
                month, year = _MONTH_NUMBER[groups[0].lower()], int(groups[1])
                day = _make_date(year, month, calendar.monthrange(year, month)[1])
            else:
                if kind == "day_month":
                    d, month, year = int(groups[0]), _MONTH_NUMBER[groups[1].lower()], groups[2]
                else:
                    month, d, year = _MONTH_NUMBER[groups[0].lower()], int(groups[1]), groups[2]
                day = (
                    _make_date(int(year), month, d) if year else _resolve_year(month, d, reference)
                )
            if day is not None:
                found.append(DateMention(day, match.start(), match.end()))
                taken.append((match.start(), match.end()))

    return sorted(found, key=lambda mention: mention.start)


def _sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Offsets of the sentence (or line) containing text[start:end], without outer spaces."""
    left = 0
    for boundary in _BOUNDARY.finditer(text, 0, start):
        left = boundary.end()
    following = _BOUNDARY.search(text, end)
    right = (
        following.start() + (0 if text[following.start()] == "\n" else 1)
        if following
        else len(text)
    )
    while left < right and text[left] in " \t\r\n-*•":  # spaces and list bullets
        left += 1
    while right > left and text[right - 1].isspace():
        right -= 1
    return left, right


def find_candidates(text: str, reference: date) -> list[Candidate]:
    """Dates whose sentence contains a deadline cue. One candidate per date and sentence."""
    candidates: list[Candidate] = []
    seen: set[tuple[date, int]] = set()
    for mention in find_dates(text, reference):
        start, end = _sentence_span(text, mention.start, mention.end)
        if end - start > MAX_QUOTE_CHARS:  # very long sentence: keep the part around the date
            start = max(start, mention.start - MAX_QUOTE_CHARS // 2)
            end = min(end, start + MAX_QUOTE_CHARS)
        quote = text[start:end]
        if not _CUES.search(quote) or (mention.day, start) in seen:
            continue
        seen.add((mention.day, start))
        candidates.append(Candidate(mention.day, quote, start, end))
    return candidates


# ── Stage 2: prompt and answer handling ──────────────────────────────────────

SYSTEM_PROMPT = """\
You find deadlines in a passage from someone's own documents.

A deadline is something the reader has to do, pay, submit, decide or cancel by a date, \
or something that expires on a date. A date that only says when something happens \
(a meeting, a flight, a birthday) is not a deadline.

You get the passage and a list of dates found in it. For each date that is a deadline, \
write what has to be done as a short instruction (at most 12 words), in the language of \
the passage. Use only dates from the list, exactly as written there.

Answer with JSON only, in this form:
{"deadlines": [{"date": "YYYY-MM-DD", "task": "..."}]}
If none of the dates is a deadline, answer {"deadlines": []}."""


def build_prompt(passage: str, candidates: list[Candidate]) -> str:
    lines = [f'- {c.day.isoformat()} | "{" ".join(c.quote.split())}"' for c in candidates]
    return f"Passage:\n{passage.strip()}\n\nDates found:\n" + "\n".join(lines)


def parse_answer(answer: str, candidates: list[Candidate]) -> dict[date, str]:
    """The model's deadlines as {date: task}. Anything not in the candidate list is dropped,
    so the model can describe a deadline but can never invent or change its date."""
    start, end = answer.find("{"), answer.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        items = json.loads(answer[start : end + 1]).get("deadlines", [])
    except (json.JSONDecodeError, AttributeError):
        return {}

    allowed = {c.day for c in candidates}
    tasks: dict[date, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            day = date.fromisoformat(str(item.get("date", "")))
        except ValueError:
            continue
        task = " ".join(str(item.get("task") or "").split())
        if day in allowed and task and day not in tasks:
            tasks[day] = task[:200]
    return tasks
