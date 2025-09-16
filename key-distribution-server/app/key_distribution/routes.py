import asyncio
from typing import Dict, Tuple

from app.config import crypto_context
from fastapi import APIRouter
from fastapi.responses import Response as FastAPIResponse
from openfhe import BINARY, Serialize

# Create a router
router = APIRouter()

_store: Dict[str, Tuple[bytes, str]] = {}
_lock = asyncio.Lock()


@router.post("/generate-keys")
async def generate_keys():  # request: Request, _: None = Depends(require_preshared_secret)):
    async with _lock:
        keys = crypto_context.KeyGen()
        _store["public_key"] = (Serialize(keys.publicKey, BINARY), "public.key")
        _store["secret_key"] = (Serialize(keys.secretKey, BINARY), "secret.key")

    return {"message": "Keys generated successfully"}


@router.get("/download/{key_type}")
async def download_key(key_type: str):  # _: None = Depends(require_preshared_secret)):
    """
    Endpoint to download a generated key (public or secret).
    """
    assert key_type in ["public_key", "secret_key"], "Invalid key type"

    async with _lock:
        if key_type not in _store:
            return FastAPIResponse(
                content=f"{key_type} not found".encode(),
                status_code=204,
                media_type="text/plain",
            )
        key_bytes, filename = _store[key_type]

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(len(key_bytes)),
        "Cache-Control": "no-store",
    }
    return FastAPIResponse(
        content=key_bytes, media_type="application/octet-stream", headers=headers
    )


@router.delete("/clear-keys")
async def clear_keys():  # _: None = Depends(require_preshared_secret)):
    """
    Endpoint to clear stored keys.
    """
    async with _lock:
        _store.clear()
    return {"message": "Stored keys cleared successfully"}
