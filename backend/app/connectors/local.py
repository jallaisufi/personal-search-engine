"""Local folder connector: finds supported files and detects changes cheaply.

Change detection is two-step: an unchanged mtime + size means "skip" without
reading the file; otherwise the content hash decides whether anything really changed.
"""

import hashlib
import mimetypes
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.ingest.extract import SUPPORTED_EXTENSIONS


@dataclass(frozen=True)
class LocalFile:
    path: Path
    external_id: str  # path relative to the root, stable across restarts
    modified_at: datetime
    size: int

    @property
    def title(self) -> str:
        return self.path.name

    @property
    def mime_type(self) -> str | None:
        return mimetypes.guess_type(self.path.name)[0]


def scan(root: Path) -> Iterator[LocalFile]:
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue  # skip hidden files and folders
        stat = path.stat()
        yield LocalFile(
            path=path,
            external_id=path.relative_to(root).as_posix(),
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            size=stat.st_size,
        )


def content_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
