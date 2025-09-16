import asyncio
import hashlib
import logging
from typing import List, Tuple

from app.config import NUM_CLIENTS
from app.utils import bytes_to_polynomial, file_transfer_fl, pack_chunks
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response as FastAPIResponse
from fastapi.responses import StreamingResponse
from openfhe import BINARY, Serialize
from starlette.requests import Request
from tqdm import tqdm

# Create a router
router = APIRouter()

aggregate_model: Tuple[bytes, str, int] = None
aggregate_model_lock = asyncio.Lock()

registered_clients = set()
register_client_lock = asyncio.Lock()

aggregate_completed = False
aggregate_completed_lock = asyncio.Lock()

successful_aggregation_downloads = set()
successful_aggregation_downloads_lock = asyncio.Lock()


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
                [Serialize(poly, BINARY) for poly in intermediate_aggregate_model]
            ),
            "aggregated_model",
            len(intermediate_aggregate_model),
        )
    async with aggregate_completed_lock:
        global aggregate_completed
        aggregate_completed = True

    logging.info("Aggregated model is ready for download.")


@router.get("/download-aggregate")
async def download_aggregate_model(request: Request):
    client_name = request.headers.get("X-Client-Name")

    async with aggregate_model_lock:
        global aggregate_model
        if aggregate_model is None:
            raise HTTPException(status_code=204, detail="No aggregated model available")
        data, model_id, total_chunks = aggregate_model

    # Hash and length
    sha256 = hashlib.sha256(data).hexdigest()
    headers = {
        "Content-Type": "application/octet-stream",
        "X-Model-ID": model_id,
        "X-Chunk-Total": str(total_chunks),
        "X-Content-SHA256": sha256,
        "Content-Length": str(len(data)),
    }

    # Stream to avoid huge memory spikes in intermediates
    return StreamingResponse(
        iter([data]), headers=headers, media_type="application/octet-stream"
    )


async def check_all_clients_registered() -> bool:
    async with register_client_lock:
        return len(registered_clients) == int(NUM_CLIENTS)


@router.post("/register-client")
async def register_client(request: Request):
    """
    Endpoint to register a new client for federated learning.
    """
    client_name = request.headers.get("X-Client-Name")
    if not client_name:
        raise HTTPException(status_code=400, detail="Missing X-Client-Name header")

    async with register_client_lock:
        if client_name in registered_clients:
            logging.info(f"Client {client_name} is already registered.")
        else:
            registered_clients.add(client_name)
            logging.info(f"Client {client_name} registered successfully.")

    if await check_all_clients_registered():
        logging.info("All clients have registered for federated learning.")

    return {"message": f"Client {client_name} registered successfully."}


@router.get("/registered-clients")
async def get_registered_clients():
    """
    Endpoint to get the list of registered clients.
    """
    async with register_client_lock:
        return {"registered_clients": list(registered_clients)}


@router.get("/all-clients-registered")
async def get_all_clients_registered():
    """
    Endpoint to check if all clients are registered.
    """
    all_registered = await check_all_clients_registered()
    return {"all_clients_registered": all_registered}


@router.get("/all-clients-uploaded")
async def get_all_clients_uploaded():
    """
    Endpoint to check if all clients have uploaded their models.
    """
    all_uploaded = await check_all_clients_uploaded()
    return {"all_clients_uploaded": all_uploaded}


@router.get("/model-aggregation-completed")
async def get_model_aggregation_completed():
    """
    Endpoint to check if model aggregation is completed.
    """
    async with aggregate_completed_lock:
        return {"model_aggregation_completed": aggregate_completed}


async def reset_server_state():
    async with file_transfer_fl._lock:
        file_transfer_fl._store.clear()
    async with aggregate_model_lock:
        global aggregate_model
        aggregate_model = None
    async with register_client_lock:
        registered_clients.clear()
    async with aggregate_completed_lock:
        global aggregate_completed
        aggregate_completed = False
    async with successful_aggregation_downloads_lock:
        successful_aggregation_downloads.clear()
    logging.info("Server state has been reset.")


@router.post("/mark-aggregation-download-complete")
async def mark_aggregation_download_complete(request: Request):
    """
    Endpoint for clients to mark that they have successfully downloaded the aggregated model.
    """
    client_name = request.headers.get("X-Client-Name")
    if not client_name:
        raise HTTPException(status_code=400, detail="Missing X-Client-Name header")

    async with successful_aggregation_downloads_lock:
        if client_name in successful_aggregation_downloads:
            logging.info(f"Client {client_name} has already marked download complete.")
        else:
            successful_aggregation_downloads.add(client_name)
            logging.info(f"Client {client_name} marked download complete.")

    if len(successful_aggregation_downloads) == int(NUM_CLIENTS):
        logging.info("All clients have successfully downloaded the aggregated model.")
        asyncio.create_task(reset_server_state())

    return {"message": f"Client {client_name} marked download complete."}
