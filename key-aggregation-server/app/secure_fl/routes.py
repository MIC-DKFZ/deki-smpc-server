import asyncio
import io
import logging
from io import BytesIO
from typing import Dict, Optional

import torch
from app.config import (
    DEVICE,
    NUM_CLIENTS,
    aggregated_state_dict,
    aggregated_state_dict_lock,
    file_transfer_fl,
)
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response as FastAPIResponse
from starlette.requests import Request

# Create a router
router = APIRouter()


@router.put("/upload")
async def upload_model(request: Request):
    """
    Endpoint to upload the model for secure federated learning.
    """

    client_name = request.headers.get("X-Client-Name")

    model_id = f"fl:{client_name}:weights"

    response = await file_transfer_fl.upload_artifact(
        model_id=model_id, request=request
    )

    # After upload, check if all clients have uploaded and trigger aggregation if so
    asyncio.create_task(aggregate_models_if_ready())

    logging.info(f"Shielded model uploaded by {client_name}")

    return response


async def aggregate_models_if_ready():
    """
    Aggregate all uploaded models: sum their tensors, store as 'model:final', and delete all model keys from Redis.
    Handles lz4, gzip, and non-compressed model files robustly.
    """
    intermediate_aggregated_state_dict: Optional[Dict[str, torch.Tensor]] = None

    async with file_transfer_fl._lock:
        if len(file_transfer_fl._store) == NUM_CLIENTS:
            logging.info("All models uploaded, starting aggregation...")
            for _, (data, _) in file_transfer_fl._store.items():
                state_dict = torch.load(BytesIO(data), map_location=DEVICE)

                if intermediate_aggregated_state_dict is None:
                    intermediate_aggregated_state_dict = state_dict
                else:
                    for k in intermediate_aggregated_state_dict.keys():
                        intermediate_aggregated_state_dict[k] += state_dict[k]

            async with aggregated_state_dict_lock:
                # Store the aggregated model
                global aggregated_state_dict
                aggregated_state_dict = intermediate_aggregated_state_dict

            # Empty the store
            file_transfer_fl._store.clear()

            logging.info("Model aggregation complete")
        else:
            logging.info(
                f"Waiting for {NUM_CLIENTS - len(file_transfer_fl._store)} more clients to upload."
            )


@router.get("/download")
async def retrieve_model():
    """
    Endpoint to retrieve the final accumulated model.
    """
    async with aggregated_state_dict_lock:
        global aggregated_state_dict
        if aggregated_state_dict is None:
            raise HTTPException(
                status_code=404,
                detail="Final model not found.",
            )

        bio = io.BytesIO()
        torch.save(aggregated_state_dict, bio, _use_new_zipfile_serialization=True)
        data = bio.getvalue()

        headers = {
            "Content-Disposition": f'attachment; filename="final.bin"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
        }

        return FastAPIResponse(
            content=data, media_type="application/octet-stream", headers=headers
        )
