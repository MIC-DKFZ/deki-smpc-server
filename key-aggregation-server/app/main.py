import logging
import os
import threading
from hashlib import sha256

import bcrypt
import redis
import torch
from fastapi import FastAPI, HTTPException
from models import KeyClientRegistration
from starlette.requests import Request

logging.basicConfig(level=logging.INFO)

# Environment variables
NUM_CLIENTS = int(os.environ.get("NUM_CLIENTS", 3))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
PRESHARED_SECRET = os.environ.get("PRESHARED_SECRET", "my_secure_presHared_secret_123!")

# Redis connection
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
R = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)

# Device
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# First hash the pre-shared secret with SHA-256, then bcrypt the result
sha256_hash = sha256(PRESHARED_SECRET.encode()).hexdigest().encode()
HASHED_PRESHARED_SECRET = bcrypt.hashpw(sha256_hash, bcrypt.gensalt())

# # State of the server
# R.sadd("server:states", "waiting", "ready", "aggregating", "finished")
# R.set("server:current_state", "waiting")

# Delete the preshared secret from memory
del PRESHARED_SECRET
del sha256_hash

logging.info(f"Number of clients: {NUM_CLIENTS}")
logging.info(f"Host: {HOST}")
logging.info(f"Port: {PORT}")
logging.info(f"Device: {DEVICE}")

app = FastAPI()

# data_lock = threading.Lock()


# async def rotate_state():
#     current_state = R.get("server:current_state")
#     states = list(R.smembers("server:states"))
#     current_index = states.index(current_state)
#     next_index = (current_index + 1) % len(states)
#     next_state = states[next_index]
#     R.set("server:current_state", next_state)
#     logging.info(f"Server state: {next_state}")
#     return next_state


@app.post("/register", response_model=KeyClientRegistration)
async def register(registration_data: KeyClientRegistration, request: Request):
    if not bcrypt.checkpw(
        registration_data.preshared_secret.encode(), HASHED_PRESHARED_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Check if the number of registered clients has reached the expected number
    registered_clients = R.scard("clients:registered")

    if registered_clients == NUM_CLIENTS:
        raise HTTPException(
            status_code=400, detail="All clients have already registered."
        )

    client_ip = request.client.host
    client_name = registration_data.client_name

    logging.info(f"Registering client {client_name}")
    logging.info(f"Client ip address: {client_ip}")

    # Push the registered client to the Redis set
    res = R.sadd("clients:registered", client_name)

    # if the client is not already registered, add the client info to the Redis hash
    if res > 0:
        R.hset(
            f"clients:info:{client_name}",
            mapping={
                "ip_address": client_ip,
                "client_name": client_name,
            },
        )
        logging.info(f"Client {client_name} registered")
    else:
        logging.warning(f"Client {client_name} already registered")

    # Check if the number of registered clients has reached the expected number
    registered_clients = R.scard("clients:registered")

    if registered_clients == NUM_CLIENTS:
        logging.info(f"All {registered_clients} clients registered.")
        logging.info("Starting key aggregation...")
    else:
        logging.info(
            f"Waiting for all ({registered_clients}/{NUM_CLIENTS}) clients to register..."
        )

    return registration_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
