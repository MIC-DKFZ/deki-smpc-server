"""Immutable filesystem artifact storage with streaming limits."""

import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredObject:
    object_id: str
    path: str
    digest: str
    size: int


class FilesystemArtifactStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        root.mkdir(parents=True, exist_ok=True)

    def _sync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def put_stream(self, chunks: AsyncIterator[bytes]) -> StoredObject:
        object_id = str(uuid.uuid4())
        temporary, final = self.root / f".{object_id}.tmp", self.root / object_id
        digest, size = hashlib.sha256(), 0
        try:
            with temporary.open("xb") as output:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError("artifact exceeds configured byte limit")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, final)
            self._sync_directory()
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return StoredObject(object_id, str(final), digest.hexdigest(), size)

    def put_bytes(self, data: bytes) -> StoredObject:
        if len(data) > self.max_bytes:
            raise ValueError("artifact exceeds configured byte limit")
        object_id = str(uuid.uuid4())
        temporary, final = self.root / f".{object_id}.tmp", self.root / object_id
        with temporary.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, final)
        self._sync_directory()
        return StoredObject(object_id, str(final), hashlib.sha256(data).hexdigest(), len(data))

    def delete(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)
        self._sync_directory()
