import asyncio
import gzip
import io
import json
import logging
import os
import secrets
from hashlib import sha256
from io import BytesIO

import bcrypt
import redis
import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from models import CheckForTaskRequest, KeyClientRegistration
from starlette.requests import Request
from utils import ActiveTasks, ActiveTasksPhase2

logging.basicConfig(level=logging.INFO)

# Environment variables
NUM_CLIENTS = int(os.environ.get("NUM_CLIENTS", 3))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8080))
PRESHARED_SECRET = os.environ.get("PRESHARED_SECRET", "my_secure_presHared_secret_123!")
WEIGHT_TTL = int(os.environ.get("WEIGHT_TTL", 600))  # seconds

# Redis connection
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
R = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
R_BINARY = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT, db=0
)  # decode_responses=False is default


# Device
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# First hash the pre-shared secret with SHA-256, then bcrypt the result
sha256_hash = sha256(PRESHARED_SECRET.encode()).hexdigest().encode()
HASHED_PRESHARED_SECRET = bcrypt.hashpw(sha256_hash, bcrypt.gensalt())

# Delete the preshared secret from memory
del PRESHARED_SECRET
del sha256_hash

assert NUM_CLIENTS >= 3, "NUM_CLIENTS must be greater than 3"

logging.info(f"Number of clients: {NUM_CLIENTS}")
logging.info(f"Host: {HOST}")
logging.info(f"Port: {PORT}")
logging.info(f"Device: {DEVICE}")

app = FastAPI()
tasks_phase_1 = ActiveTasks()
tasks_phase_2 = ActiveTasksPhase2()

# Store Phase in Redis
R.set("phase", 1)


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

    # Pull the initial active tasks from the Redis queues
    group_keys = [key for key in R.keys("queue:aggregation:initial:group_*")]

    for group_queue in group_keys:
        task_data = R.lpop(group_queue)
        if task_data:
            task = json.loads(task_data)
            tasks_phase_1.add_task(task["from"], task["to"], group_queue)

    # Load the tasks for phase 2
    task_data = {}
    for key in sorted(R.keys("queue:aggregation:groups")):
        task_data[key] = R.lrange(key, 0, -1)
    tasks_phase_2.load_tasks_from_json(
        task_data=task_data, queue_name="queue:aggregation:groups"
    )


async def schedule_next_task(queue):
    task_data = R.lpop(queue)
    if task_data:
        task = json.loads(task_data)
        tasks_phase_1.add_task(task["from"], task["to"], queue)
    else:
        # Delete the queue if it's empty
        R.delete(queue)


@app.get("/tasks/participants")
async def get_phase_participants():
    return JSONResponse(
        content={
            "phase_1_clients": list(R.smembers("clients:registered")),
            "phase_2_clients": list(tasks_phase_2.phase_2_clients),
        }
    )


@app.get("/aggregation/phase/{phase_id}/check_for_task")
async def check_for_task(check_for_task_request: CheckForTaskRequest, phase_id: int):
    assert phase_id in [1, 2], "Invalid phase ID. Must be 1 or 2."

    if phase_id == 1:

        task = tasks_phase_1.check_for_task(check_for_task_request.client_name)

        if task:
            return task
        else:
            return Response(status_code=204)
    else:
        task = tasks_phase_2.check_for_task(check_for_task_request.client_name)

        if task:
            return task
        else:
            return Response(status_code=204)


@app.post("/aggregation/phase/{phase_id}/upload")
async def upload_weights(
    phase_id: int, key: UploadFile = File(...), client_name: str = Form(...)
):
    assert phase_id in [1, 2], "Invalid phase ID. Must be 1 or 2."

    # # --- Load and print torch model ---
    # contents = await key.read()
    # buffer = io.BytesIO(contents)

    # with gzip.GzipFile(fileobj=buffer, mode="rb") as f:
    #     state_dict: dict = torch.load(f, map_location="cpu")  # map_location if needed

    # print(f"Loaded state_dict from {client_name}:")
    # for k, v in state_dict.items():
    #     print(f"{k}: {v.shape}")

    contents = await key.read()
    # buffer = io.BytesIO(contents)

    # with gzip.GzipFile(fileobj=buffer, mode="rb") as f:
    #     state_dict: dict = torch.load(f, map_location="cpu")  # map_location if needed

    # --- Store in Redis ---

    if phase_id == 1:
        task = tasks_phase_1.check_for_task(client_name)["task"]
        sender = task["from"]
        receiver = task["to"]
        redis_key = f"phase:{phase_id}:weights:{sender}:{receiver}"
        R_BINARY.set(redis_key, contents)
        tasks_phase_1.complete_task(client_name)
    else:
        task = tasks_phase_2.check_for_task(client_name)["task"]
        sender = task["from"]
        receiver = task["to"]
        redis_key = f"phase:{phase_id}:weights:{sender}:{receiver}"
        R_BINARY.set(redis_key, contents)
        tasks_phase_2.complete_task(client_name)
    return {"message": f"Weights uploaded for {client_name}"}


@app.get("/aggregation/phase/{phase_id}/download")
async def download_weights(check_for_task_request: CheckForTaskRequest, phase_id: int):
    assert phase_id in [1, 2], "Invalid phase ID. Must be 1 or 2."

    # redis_key = f"phase:{phase_id}:weights:{check_for_task_request.client_name}"
    # contents = R.get(redis_key)

    # if contents is None:
    #     raise HTTPException(
    #         status_code=404,
    #         detail=f"Weights not found for client {check_for_task_request.client_name}",
    #     )

    if phase_id == 1:
        task = tasks_phase_1.check_for_task(check_for_task_request.client_name)["task"]
        sender = task["from"]
        receiver = task["to"]
        redis_key = f"phase:{phase_id}:weights:{sender}:{receiver}"
        contents = R_BINARY.get(redis_key)
        if contents is None:
            raise HTTPException(
                status_code=404,
                detail=f"Weights not found for client {check_for_task_request.client_name}",
            )

        task_queue = tasks_phase_1.complete_task(check_for_task_request.client_name)
        asyncio.create_task(schedule_next_task(task_queue["queue"]))
    else:
        task = tasks_phase_2.check_for_task(check_for_task_request.client_name)["task"]
        sender = task["from"]
        receiver = task["to"]
        redis_key = f"phase:{phase_id}:weights:{sender}:{receiver}"
        contents = R_BINARY.get(redis_key)
        if contents is None:
            raise HTTPException(
                status_code=404,
                detail=f"Weights not found for client {check_for_task_request.client_name}",
            )
        tasks_phase_2.complete_task(check_for_task_request.client_name)

    # Create a stream to return the file
    filename = f"{check_for_task_request.client_name}_weights.pt.gz"
    return StreamingResponse(
        io.BytesIO(contents),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/redis/keys")
async def get_redis_keys():
    return R.keys("phase:*")


@app.get("/tasks")
async def debug_tasks():
    data = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        data[key] = R.lrange(key, 0, -1)
    return data


@app.post("/tasks/reset")
async def reset_tasks():
    # Clear all tasks and queues
    R.delete("queue:aggregation:initial")
    R.delete("queue:aggregation:groups")
    tasks_phase_1.clear_tasks()
    tasks_phase_2.clear_tasks()

    asyncio.create_task(define_aggregation_flow())

    return {"message": "All tasks and queues have been reset."}


@app.get("/aggregation/phase/{phase_id}/active_tasks")
async def get_active_tasks(phase_id: int):
    assert phase_id in [1, 2], "Invalid phase ID. Must be 1 or 2."
    if phase_id == 1:
        active_tasks = tasks_phase_1.get_all_tasks()
    else:
        active_tasks = tasks_phase_2.get_all_tasks()
    return active_tasks


@app.get("/redis/queues")
async def get_redis_queues():
    queues = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        queues[key] = R.lrange(key, 0, -1)
    return queues


@app.get("/redis/queues/{queue_name}")
async def get_redis_queue(queue_name: str):
    queue = R.lrange(f"queue:aggregation:{queue_name}", 0, -1)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")
    return {queue_name: queue}


@app.post("/register", response_model=KeyClientRegistration)
async def register(registration_data: KeyClientRegistration, request: Request):
    if not bcrypt.checkpw(
        registration_data.preshared_secret.encode(), HASHED_PRESHARED_SECRET
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Check if the number of registered clients has reached the expected number
    registered_clients = R.scard("clients:registered")

    client_ip = request.client.host
    client_name = registration_data.client_name

    # Check if the client is already registered
    if R.sismember("clients:registered", client_name):
        return registration_data

    if registered_clients == NUM_CLIENTS:
        raise HTTPException(
            status_code=400, detail="All clients have already registered."
        )

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
        asyncio.create_task(define_aggregation_flow())
    else:
        logging.info(
            f"Waiting for all ({registered_clients}/{NUM_CLIENTS}) clients to register..."
        )

    return registration_data


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
