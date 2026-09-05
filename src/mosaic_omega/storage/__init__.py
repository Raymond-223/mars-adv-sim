"""Single local object-store implementation shared by Memory and MessageTopology.

The same filesystem backend supports two access patterns without duplicating
persistence logic:

* named objects for snapshots (`put_bytes/get_bytes/list_keys`);
* content-addressed objects for large message/evidence payloads (`put/get`).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Protocol
import uuid


@dataclass(frozen=True)
class StoredObject:
    uri: str
    sha256: str
    size_bytes: int
    media_type: str


class ContentAddressedObjectStore(Protocol):
    def put(self, data: bytes, *, media_type: str = "application/octet-stream") -> StoredObject: ...
    def get(self, uri: str) -> bytes: ...
    def verify_hash(self, uri: str) -> bool: ...


class LocalObjectStore:
    """Workspace-scoped file object store with optional content addressing."""

    def __init__(self, root_dir: str | Path):
        self.root = Path(root_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized = str(key).replace("\\", "/").lstrip("/")
        path = (self.root / normalized).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("object-store key escapes configured root")
        return path

    @staticmethod
    def _digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    # Named-object API used by memory snapshots.
    def put_bytes(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return str(path)

    def get_bytes(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        path.unlink()
        return True

    def uri(self, key: str) -> str:
        return str(self._path(key))

    def list_keys(self, prefix: str = "") -> list[str]:
        base = self._path(prefix) if prefix else self.root
        if base.is_file():
            return [str(base.relative_to(self.root)).replace("\\", "/")]
        if not base.exists():
            return []
        items = [path for path in base.rglob("*") if path.is_file()]
        items.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        return [str(path.relative_to(self.root)).replace("\\", "/") for path in items]

    # Content-addressed API used by message topology / large evidence payloads.
    def put(self, data: bytes, *, media_type: str = "application/octet-stream") -> StoredObject:
        if not isinstance(data, bytes):
            raise TypeError("object store accepts bytes")
        digest = self._digest(data)
        key = f"sha256/{digest[:2]}/{digest}"
        target = self._path(key)
        if not target.exists():
            self.put_bytes(key, data)
        return StoredObject(
            uri=f"cas://sha256/{digest}",
            sha256=digest,
            size_bytes=len(data),
            media_type=media_type,
        )

    @staticmethod
    def _cas_digest(uri: str) -> str:
        prefix = "cas://sha256/"
        if not uri.startswith(prefix):
            raise ValueError("unsupported object URI")
        digest = uri[len(prefix):]
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("invalid sha256 digest")
        return digest

    def get(self, uri: str) -> bytes:
        digest = self._cas_digest(uri)
        data = self._path(f"sha256/{digest[:2]}/{digest}").read_bytes()
        if self._digest(data) != digest:
            raise ValueError("stored object failed hash verification")
        return data

    def verify_hash(self, uri: str) -> bool:
        try:
            digest = self._cas_digest(uri)
            return self._digest(self._path(f"sha256/{digest[:2]}/{digest}").read_bytes()) == digest
        except (OSError, ValueError):
            return False
