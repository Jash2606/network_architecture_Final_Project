"""The file server's domain: which file, under the root, does a path name?

This is the only place that touches the document root, so it is the only
place that has to get path traversal right.
"""
from __future__ import annotations

import mimetypes
import os
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from typing import Iterator
from urllib.parse import unquote

# Explicit, so the answer does not depend on the Windows registry or /etc/mime.types.
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".pdf": "application/pdf",
}


class BadPath(ValueError):
    """The path is not a well-formed path under the root (-> 400)."""


class NotFound(LookupError):
    """Nothing servable lives at that path (-> 404)."""


@dataclass(frozen=True)
class FileResource:
    path: Path
    size: int
    mtime: float
    mtime_ns: int

    @property
    def content_type(self) -> str:
        suffix = self.path.suffix.lower()
        if suffix in CONTENT_TYPES:
            return CONTENT_TYPES[suffix]
        guessed, _ = mimetypes.guess_type(self.path.name)
        return guessed or "application/octet-stream"

    @property
    def last_modified(self) -> str:
        return formatdate(self.mtime, usegmt=True)

    @property
    def etag(self) -> str:
        return f'"{self.size:x}-{self.mtime_ns:x}"'

    def chunks(self, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
        """The file's bytes, never more than `size` of them."""
        remaining = self.size
        with open(self.path, "rb") as handle:
            while remaining > 0:
                chunk = handle.read(min(chunk_size, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk


class FileStore:
    INDEX = "index.html"

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise NotADirectoryError(root)

    def lookup(self, raw_path: str) -> FileResource:
        """Map a request path such as '/docs/a%20b.html?x=1' to a file."""
        relative = self._relative_parts(raw_path)
        candidate = self.root.joinpath(*relative)
        if candidate.is_dir():
            candidate = candidate / self.INDEX
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            raise NotFound(raw_path) from None
        # A symlink (or a Windows drive letter) may still lead out of the root.
        if os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise NotFound(raw_path)
        if not resolved.is_file():
            raise NotFound(raw_path)
        stat = resolved.stat()
        return FileResource(resolved, stat.st_size, stat.st_mtime, stat.st_mtime_ns)

    @staticmethod
    def _relative_parts(raw_path: str) -> list:
        path = raw_path.split("?", 1)[0].split("#", 1)[0]
        if not path.startswith("/"):
            raise BadPath("path must start with '/'")
        try:
            decoded = unquote(path, errors="strict")
        except UnicodeDecodeError:
            raise BadPath("percent-escapes are not valid UTF-8") from None
        if "\x00" in decoded or "\\" in decoded:
            raise BadPath("path contains NUL or backslash")
        parts: list = []
        for segment in decoded.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if not parts:
                    raise BadPath("path climbs above the root")
                parts.pop()
                continue
            if ":" in segment and os.name == "nt":
                raise BadPath("drive letters and streams are not paths")
            parts.append(segment)
        return parts
