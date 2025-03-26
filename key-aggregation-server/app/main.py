import asyncio
import json
import logging
import os
import secrets
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

# Delete the preshared secret from memory
del PRESHARED_SECRET
del sha256_hash

logging.info(f"Number of clients: {NUM_CLIENTS}")
logging.info(f"Host: {HOST}")
logging.info(f"Port: {PORT}")
logging.info(f"Device: {DEVICE}")

app = FastAPI()


async def __secure_shuffle(array: list) -> list:
    array = array[:]  # avoid modifying original list
    n = len(array)
    for i in range(n - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        array[i], array[j] = array[j], array[i]
    return array


async def __form_initial_groups(registered_clients):
    initial_groups = {}
    num_clients = len(registered_clients)
    num_full_groups = num_clients // 3
    leftover = num_clients % 3

    group_idx = 0
    client_idx = 0

    for _ in range(num_full_groups):
        initial_groups[group_idx] = registered_clients[client_idx : client_idx + 3]
        client_idx += 3
        group_idx += 1

    if leftover == 1:
        initial_groups[group_idx - 1].append(registered_clients[client_idx])
    elif leftover == 2:
        initial_groups[group_idx - 2].append(registered_clients[client_idx])
        initial_groups[group_idx - 1].append(registered_clients[client_idx + 1])

    return initial_groups


async def define_aggregation_flow():
    # Clear old queues
    R.delete("queue:aggregation:initial")
    R.delete("queue:aggregation:groups")

    # Get and shuffle registered clients
    registered_clients = list(R.smembers("clients:registered"))
    registered_clients = await __secure_shuffle(registered_clients)

    # Form groups
    initial_groups = await __form_initial_groups(registered_clients)

    group_representatives = []

    # Phase 1: Intra-group ring aggregation (per-group queues)
    for group_id, clients in initial_groups.items():
        group_queue = f"queue:aggregation:initial:group_{group_id}"

        # Clear the group-specific queue in case it's reused
        R.delete(group_queue)

        n = len(clients)
        for i in range(n):
            task = {"from": clients[i], "to": clients[(i + 1) % n]}
            R.rpush(group_queue, json.dumps(task))

        # First client is the representative for group aggregation
        group_representatives.append(clients[0])

    # Phase 2: Inter-group aggregation (binary tree style)
    current_level = group_representatives

    while len(current_level) > 1:
        next_level = []
        for i in range(0, len(current_level), 2):
            if i + 1 < len(current_level):
                task = {"from": current_level[i], "to": current_level[i + 1]}
                R.rpush("queue:aggregation:groups", json.dumps(task))
                next_level.append(current_level[i + 1])
            else:
                next_level.append(current_level[i])
        current_level = next_level


async def process_aggregation_tasks():
    # --- Phase 1: Intra-group ring aggregation ---
    group_keys = [key for key in R.keys("queue:aggregation:initial:group_*")]
    group_keys.sort()  # Optional: for consistent ordering

    for group_queue in group_keys:
        logging.info(f"Processing {group_queue}")
        while True:
            task_data = R.lpop(group_queue)
            if not task_data:
                break  # No more tasks in this group
            task = json.loads(task_data)
            await handle_task(task, phase="initial", queue=group_queue)

    # --- Phase 2: Inter-group aggregation ---
    logging.info("Processing queue:aggregation:groups")
    while True:
        task_data = R.lpop("queue:aggregation:groups")
        if not task_data:
            break
        task = json.loads(task_data)
        await handle_task(task, phase="group", queue="queue:aggregation:groups")

    logging.info("All aggregation tasks completed.")


async def handle_task(task, phase, queue):
    sender = task["from"]
    receiver = task["to"]
    logging.info(f"[{phase.upper()}] {sender} → {receiver} (from {queue})")
    # Simulate actual work
    await asyncio.sleep(0.1)


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
        await define_aggregation_flow()
        await process_aggregation_tasks()
    else:
        logging.info(
            f"Waiting for all ({registered_clients}/{NUM_CLIENTS}) clients to register..."
        )

    return registration_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
