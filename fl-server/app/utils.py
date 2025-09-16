import asyncio
import logging
import os
import struct
import tempfile
from typing import Dict, List, Tuple

from fastapi import Request, Response
from openfhe import BINARY, DeserializeCiphertext

logging.basicConfig(level=logging.INFO)

_MAGIC = b"CHNK"  # 4 bytes
_VERSION = 1  # 1 byte
_HDR_FMT = ">4sB"  # magic, version
_CNT_FMT = ">I"  # uint32: number of chunks
_LEN_FMT = ">Q"  # uint64: per-chunk length


async def pack_chunks(chunks):
    """
    Pack an iterable of bytes-like objects into a single bytes payload.

    Wire format:
      [MAGIC=CHNK][VERSION=1][COUNT:u32][ (LEN:u64)(DATA) ... repeated COUNT times ]
    """
    # Normalize to bytes and validate types early
    bchunks = []
    for idx, ch in enumerate(chunks):
        if not isinstance(ch, (bytes, bytearray, memoryview)):
            raise TypeError(f"Chunk {idx} is not bytes-like")
        bchunks.append(bytes(ch))

    parts = []
    parts.append(struct.pack(_HDR_FMT, _MAGIC, _VERSION))
    parts.append(struct.pack(_CNT_FMT, len(bchunks)))
    for ch in bchunks:
        parts.append(struct.pack(_LEN_FMT, len(ch)))
        parts.append(ch)
    return b"".join(parts)


async def unpack_chunks(blob):
    """
    Unpack a bytes payload produced by pack_chunks back into a list of bytes.
    Raises ValueError if the blob is malformed.
    """
    mv = memoryview(blob)
    offset = 0

    # Header
    if len(mv) < struct.calcsize(_HDR_FMT) + struct.calcsize(_CNT_FMT):
        raise ValueError("Blob too small")

    magic, version = struct.unpack_from(_HDR_FMT, mv, offset)
    offset += struct.calcsize(_HDR_FMT)

    if magic != _MAGIC:
        raise ValueError("Bad magic header")
    if version != _VERSION:
        raise ValueError(f"Unsupported version {version}")

    (count,) = struct.unpack_from(_CNT_FMT, mv, offset)
    offset += struct.calcsize(_CNT_FMT)

    chunks = []
    for i in range(count):
        if offset + struct.calcsize(_LEN_FMT) > len(mv):
            raise ValueError(f"Truncated before chunk {i} length")
        (length,) = struct.unpack_from(_LEN_FMT, mv, offset)
        offset += struct.calcsize(_LEN_FMT)

        end = offset + length
        if end > len(mv):
            raise ValueError(f"Truncated in chunk {i} data")
        chunks.append(bytes(mv[offset:end]))
        offset = end

    if offset != len(mv):
        raise ValueError("Trailing bytes after last chunk")

    return chunks


async def bytes_to_polynomial(data: bytes):
    with tempfile.TemporaryDirectory() as tmpdirname:
        with open(os.path.join(tmpdirname, "poly.txt"), "wb") as f:
            f.write(data)
        c, _ = DeserializeCiphertext(os.path.join(tmpdirname, "poly.txt"), BINARY)
    return c


class FileTransfer:
    def __init__(self):
        self._store: Dict[str, Tuple[List[bytes], int]] = {}
        self._lock = asyncio.Lock()

    async def upload_artifact(
        self, model_id: str, request: Request, chunk_total: int
    ) -> Response:
        buf = bytearray()
        async for chunk in request.stream():
            if chunk:
                buf.extend(chunk)

        # content_type = request.headers.get("content-type") or "application/octet-stream"

        data = bytes(buf)

        chunks = await unpack_chunks(data)

        async with self._lock:
            self._store[model_id] = (
                chunks,
                chunk_total,
            )

        return Response(status_code=204)


file_transfer_fl = FileTransfer()
