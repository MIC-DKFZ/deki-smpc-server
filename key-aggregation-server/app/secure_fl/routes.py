import asyncio
import gzip
import io
import json
import logging
import os
import secrets
import time
from hashlib import sha256
from io import BytesIO

import bcrypt
import redis
import torch
from app.config import NUM_CLIENTS, R_BINARY
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from models import CheckForTaskRequest, KeyClientRegistration
from starlette.requests import Request
from utils import ActiveTasks, ActiveTasksPhase2

# Create a router
router = APIRouter()


@router.post("/upload")
async def upload_model(model: UploadFile = File(...), client_name: str = Form(...)):
    """
    Endpoint to upload the model for secure federated learning.
    """
    contents = await model.read()
    redis_key = f"model:{client_name}"
    R_BINARY.set(redis_key, contents)

    # After upload, check if all clients have uploaded and trigger aggregation if so
    asyncio.create_task(aggregate_models_if_ready())

    return {"message": f"Model uploaded by {client_name}"}


async def aggregate_models_if_ready():
    """
    Dummy aggregation: if all NUM_CLIENTS models are uploaded, set the first as final.
    """
    try:
        # List all model:* keys except 'model:final'
        keys = [k for k in R_BINARY.keys("model:*") if k != b"model:final"]
        if len(keys) == NUM_CLIENTS:
            # Only aggregate if 'model:final' does not exist
            if not R_BINARY.exists("model:final"):
                # Use the first uploaded model as the final one
                first_key = keys[0]
                model_bytes = R_BINARY.get(first_key)
                R_BINARY.set("model:final", model_bytes)
                logging.info(
                    f"Aggregated {NUM_CLIENTS} models. Set {first_key} as final."
                )
    except Exception as e:
        logging.error(f"Error in aggregate_models_if_ready: {e}")


@router.get("/download")
async def retrieve_model():
    """
    Endpoint to retrieve the final accumulated model.
    """
    content = R_BINARY.get(f"model:final")

    if content is None:
        raise HTTPException(
            status_code=404,
            detail="Final model not found.",
        )

    filename = f"final_model.pt.gz"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
