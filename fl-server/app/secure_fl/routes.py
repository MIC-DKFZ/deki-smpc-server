import asyncio
import logging
from typing import List, Tuple

from app.config import NUM_CLIENTS
from app.utils import (
    bytes_to_polynomial,
    file_transfer_fl,
    pack_chunks,
    polynomial_to_bytes,
)
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response as FastAPIResponse
from starlette.requests import Request
from tqdm import tqdm

# Create a router
router = APIRouter()

aggregate_model: Tuple[bytes, str, int] = None
aggregate_model_lock = asyncio.Lock()


async def check_all_clients_uploaded() -> bool:
    async with file_transfer_fl._lock:
        return len(file_transfer_fl._store) == int(NUM_CLIENTS) and all(
            len(chunks_list) == int(chunk_total)
            for chunks_list, chunk_total in file_transfer_fl._store.values()
        )


@router.get("/uploaded-model-keys")
async def get_uploaded_model_keys():
    """
    Endpoint to get the keys of all uploaded models.
    """
    return {"uploaded_model_keys": list(file_transfer_fl._store.keys())}


@router.put("/upload")
async def upload_model(request: Request):
    """
    Endpoint to upload the model for secure federated learning.
    """

    client_name = request.headers.get("X-Client-Name")
    chunk_total = request.headers.get("X-Chunk-Total")

    model_id = f"fl:{client_name}:weights"

    response = await file_transfer_fl.upload_artifact(
        model_id=model_id,
        request=request,
        chunk_total=chunk_total,
    )

    if await check_all_clients_uploaded():
        logging.info("All clients have uploaded their models.")
        asyncio.create_task(aggregate_models_if_ready())
    else:
        logging.info(
            f"Shielded model uploaded by {client_name}. Total chunks: {chunk_total}"
        )

    return response


async def aggregate_models_if_ready():  # TODO
    logging.info("All models uploaded, starting aggregation...")

    intermediate_aggregate_model: List[bytes] = []
    first = True

    async with file_transfer_fl._lock:
        for _, (chunks_list, _) in tqdm(
            file_transfer_fl._store.items(), desc="Aggregating models"
        ):
            if first:
                intermediate_aggregate_model = [
                    await bytes_to_polynomial(data) for data in chunks_list
                ]
                first = False
                continue
            for key, data in enumerate(chunks_list):
                poly = await bytes_to_polynomial(data)
                cc = poly.GetCryptoContext()
                intermediate_aggregate_model[key] = cc.EvalAdd(
                    intermediate_aggregate_model[key], poly
                )

    logging.info("Aggregation complete.")

    async with aggregate_model_lock:
        global aggregate_model
        aggregate_model = (
            await pack_chunks(
                [
                    await polynomial_to_bytes(poly)
                    for poly in intermediate_aggregate_model
                ]
            ),
            "aggregated_model",
            len(intermediate_aggregate_model),
        )

    logging.info("Aggregated model is ready for download.")


@router.get("/download-aggregate")
async def download_aggregate_model(request: Request):
    """
    Endpoint to download the aggregated model for secure federated learning.
    """
    client_name = request.headers.get("X-Client-Name")

    async with aggregate_model_lock:
        global aggregate_model
        if aggregate_model is None:
            raise HTTPException(status_code=404, detail="No aggregated model available")
        data, model_id, total_chunks = aggregate_model

    logging.info(f"Client {client_name} is downloading the aggregated model.")

    headers = {
        "Content-Type": "application/octet-stream",
        "X-Model-ID": model_id,
        "X-Chunk-Total": str(total_chunks),
    }

    return FastAPIResponse(content=data, headers=headers)
