import argparse
import json
import logging
import threading
from hashlib import sha256

import bcrypt
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from models import KeyClientRegistration

logging.basicConfig(level=logging.INFO)

parser = argparse.ArgumentParser(description="Flower")
parser.add_argument(
    "--num-clients",
    type=int,
    default=3,
    help="Number of clients in the federated learning setup",
)
parser.add_argument(
    "--host",
    type=str,
    default="0.0.0.0",
    help="Host IP",
)
parser.add_argument(
    "--port",
    type=int,
    default=8080,
    help="Port number",
)
parser.add_argument(
    "--preshared-secret",
    "-pss",
    type=str,
    default="my_secure_presHared_secret_123!",
    help="Pre-shared secret for clients to register",
)

args = parser.parse_args()

NUM_CLIENTS = args.num_clients
HOST = args.host
PORT = args.port
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# First hash the pre-shared secret with SHA-256, then bcrypt the result
sha256_hash = sha256(args.preshared_secret.encode()).hexdigest().encode()
HASHED_PRESHARED_SECRET = bcrypt.hashpw(sha256_hash, bcrypt.gensalt())

# Delete the preshared secret from memory
del args.preshared_secret

logging.info(f"Number of clients: {NUM_CLIENTS}")
logging.info(f"Host: {HOST}")
logging.info(f"Port: {PORT}")
logging.info(f"Device: {DEVICE}")

app = FastAPI()

# Data storage and lock
data = {
    "sum": None,  # Initialized as None; will store tensor data
    "current_client": 0,
    "total_clients": NUM_CLIENTS,
    "clients_logged_in": set(),
    "number_of_downloads": 0,
    "final_sum_is_set": False,
}
data_lock = threading.Lock()


@app.post("/register", response_model=KeyClientRegistration)
async def register(registration_data: KeyClientRegistration):
    if not bcrypt.checkpw(
        registration_data.preshared_secret.encode(), HASHED_PRESHARED_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return registration_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
