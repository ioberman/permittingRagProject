"""Local, content-addressable file storage.

Files are stored and keyed by their SHA-256 hash rather than by name, mirroring
the clause-level dedup in app/models.py: identical bytes uploaded twice land at
the same path, and the hash doubles as an integrity check.

Layout: <root>/<hash[:2]>/<hash[2:4]>/<hash>_<original_filename>

Kept as a small save/load interface so a future S3-backed implementation can
be swapped in without touching call sites.
"""

import hashlib
import os
from pathlib import Path


class LocalFileStorage:
    def __init__(self, root: str | None = None):
        self.root = Path(root or os.environ.get("STORAGE_ROOT", "./storage"))

    def save(self, content: bytes, filename: str) -> tuple[str, str]:
        """Writes content if not already present. Returns (uri, sha256_hash).

        uri is relative to the storage root, so it stays valid if the root
        moves or storage later points at a different backend.
        """
        file_hash = hashlib.sha256(content).hexdigest()
        relative_path = Path(file_hash[:2]) / file_hash[2:4] / f"{file_hash}_{filename}"
        full_path = self.root / relative_path

        if not full_path.exists():
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_bytes(content)

        return str(relative_path), file_hash

    def load(self, uri: str) -> bytes:
        full_path = (self.root / uri).resolve()
        if not full_path.is_relative_to(self.root.resolve()):
            raise ValueError(f"uri escapes storage root: {uri!r}")
        return full_path.read_bytes()
