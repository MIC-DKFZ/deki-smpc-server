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
from app.config import NUM_CLIENTS, R_BINARY, DEVICE
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
    Aggregate all uploaded models: sum their tensors, store as 'model:final', and delete all model keys from Redis.
    Handles lz4, gzip, and non-compressed model files robustly.
    """
    try:
        # List all model:* keys except 'model:final'
        keys = [k for k in R_BINARY.keys("model:*") if k != b"model:final"]
        if len(keys) == NUM_CLIENTS:
            # Only aggregate if 'model:final' does not exist
            if not R_BINARY.exists("model:final"):
                import lz4.frame

                models = []
                for key in keys:
                    model_bytes = R_BINARY.get(key)
                    buffer = BytesIO(model_bytes)
                    magic = buffer.read(4)
                    buffer.seek(0)
                    try:
                        if magic[:2] == b"\x1f\x8b":  # gzip
                            with gzip.GzipFile(fileobj=buffer, mode="rb") as f:
                                model_data = torch.load(f, map_location=DEVICE)
                        elif magic == b"\x04\x22\x4d\x18":  # lz4
                            with lz4.frame.open(buffer, mode="rb") as f:
                                model_data = torch.load(f, map_location=DEVICE)
                        else:  # raw torch
                            model_data = torch.load(buffer, map_location=DEVICE)
                    except Exception as e:
                        logging.error(f"Error loading model from key {key}: {e}")
                        raise
                    models.append(model_data)
                # Sum all models
                agg_model = models[0]
                for m in models[1:]:
                    for k in agg_model.keys():
                        agg_model[k] += m[k]
                # Save aggregated model to bytes (lz4)
                out_buffer = BytesIO()
                with lz4.frame.open(out_buffer, mode="wb") as f:
                    torch.save(agg_model, f)
                out_buffer.seek(0)
                R_BINARY.set("model:final", out_buffer.read())
                # Delete all model:* keys except 'model:final'
                for key in keys:
                    R_BINARY.delete(key)
                logging.info(
                    f"Aggregated {NUM_CLIENTS} models. Summed and set as final (lz4). Deleted all model keys."
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
