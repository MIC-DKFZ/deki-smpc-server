from hashlib import sha256
from config import PRESHARED_SECRET
from fastapi import HTTPException, Request


# FastAPI dependency for preshared secret validation
from fastapi import Depends


async def require_preshared_secret(request: Request):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    preshared_secret = data.get("preshared_secret")
    if (
        not preshared_secret
        or sha256(preshared_secret.encode()).hexdigest() != PRESHARED_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    # No return needed; just validates
