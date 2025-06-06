import asyncio
import io
import json
import logging
import secrets
import time

import bcrypt
from app.config import HASHED_PRESHARED_SECRET, NUM_CLIENTS, R_BINARY, R
from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from models import CheckForTaskRequest, KeyClientRegistration
from starlette.requests import Request
from utils import ActiveTasks, ActiveTasksPhase2

# Create a router
router = APIRouter()

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
    tasks_phase_2.phase_1_groups = initial_groups = await __form_initial_groups(
        registered_clients
    )

    group_representatives = []
    first_senders = []  # List to store first senders

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
        first_senders.append(clients[0])  # Add the first sender to the list

    logging.info(f"Registered clients: {registered_clients}")
    logging.info(f"Initial groups: {initial_groups}")
    logging.info(f"First senders before saving to Redis: {first_senders}")

    # Save the list of first senders in Redis
    R.set("phase:1:first_senders", json.dumps(first_senders))

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


@router.get("/tasks/participants")
async def get_phase_participants():
    return JSONResponse(
        content={
            "phase_1_clients": list(R.smembers("clients:registered")),
            "phase_2_clients": list(tasks_phase_2.phase_2_clients),
        }
    )


@router.get("/aggregation/phase/{phase_id}/check_for_task")
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


@router.post("/aggregation/phase/{phase_id}/upload")
async def upload_weights(
    phase_id: int, key: UploadFile = File(...), client_name: str = Form(...)
):
    """
    Endpoint to upload weights for a specific phase.
    """
    start_time = time.time()
    contents = await key.read()

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

    duration = time.time() - start_time
    logging.info(
        f"Upload weights for phase {phase_id} by {client_name} took {duration:.2f} seconds."
    )
    return {"message": f"Weights uploaded for {client_name}", "duration": duration}


@router.get("/aggregation/phase/{phase_id}/download")
async def download_weights(check_for_task_request: CheckForTaskRequest, phase_id: int):
    """
    Endpoint to download weights for a specific phase.
    """
    start_time = time.time()

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

    duration = time.time() - start_time
    logging.info(
        f"Download weights for phase {phase_id} by {check_for_task_request.client_name} took {duration:.2f} seconds."
    )

    filename = f"{check_for_task_request.client_name}_weights.pt.gz"
    return StreamingResponse(
        io.BytesIO(contents),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/redis/keys")
async def get_redis_keys():
    return R.keys("phase:*")


@router.get("/tasks")
async def debug_tasks():
    data = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        data[key] = R.lrange(key, 0, -1)
    return data


@router.post("/tasks/reset")
async def reset_tasks():
    # Clear all tasks and queues
    R.delete("queue:aggregation:initial")
    R.delete("queue:aggregation:groups")
    tasks_phase_1.clear_tasks()
    tasks_phase_2.clear_tasks()

    # Delete all registered clients
    R.delete("clients:registered")

    # Reset the phase in Redis
    R.set("phase", 1)

    # Clear the model weights
    for key in sorted(R.keys("phase:*:weights:*")):
        R.delete(key)
    # Clear the final sum
    R.delete("final:sum")

    # Clear the first senders
    R.delete("phase:1:first_senders")

    asyncio.create_task(define_aggregation_flow())

    return {"message": "All tasks and queues have been reset."}


@router.get("/aggregation/phase/{phase_id}/active_tasks")
async def get_active_tasks(phase_id: int):
    assert phase_id in [1, 2], "Invalid phase ID. Must be 1 or 2."
    if phase_id == 1:
        active_tasks = tasks_phase_1.get_all_tasks()
    else:
        active_tasks = tasks_phase_2.get_all_tasks()
    return active_tasks


@router.get("/redis/queues")
async def get_redis_queues():
    queues = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        queues[key] = R.lrange(key, 0, -1)
    return queues


@router.get("/redis/queues/{queue_name}")
async def get_redis_queue(queue_name: str):
    queue = R.lrange(f"queue:aggregation:{queue_name}", 0, -1)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")
    return {queue_name: queue}


@router.post("/register", response_model=KeyClientRegistration)
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


@router.post("/aggregation/final/upload")
async def upload_final_sum(
    final_sum: UploadFile = File(...), client_name: str = Form(...)
):
    """
    Endpoint to upload the final accumulated sum.
    """
    start_time = time.time()
    contents = await final_sum.read()
    redis_key = f"final:sum"
    R_BINARY.set(redis_key, contents)
    duration = time.time() - start_time
    logging.info(f"Upload final sum by {client_name} took {duration:.2f} seconds.")
    return {"message": f"Final sum uploaded by {client_name}", "duration": duration}


@router.get("/aggregation/final/download")
async def retrieve_final_sum():
    """
    Endpoint to retrieve the final accumulated sum shared by all clients.
    """
    start_time = time.time()
    content = R_BINARY.get(f"final:sum")

    if content is None:
        raise HTTPException(
            status_code=404,
            detail="Final sum not found. Please ensure it has been uploaded.",
        )

    filename = f"final_weights.pt.gz"
    duration = time.time() - start_time
    logging.info(f"Download final sum took {duration:.2f} seconds.")
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/aggregation/final/recipient")
async def get_final_sum_recipient():
    """
    Endpoint to retrieve the recipient of the final sum.
    """
    recipient = tasks_phase_2.get_last_recipient()
    return {"recipient": recipient}


@router.get("/aggregation/phase/1/first_senders")
async def is_client_first_sender():
    """
    Endpoint to check if a client is in the list of first senders.
    """
    first_senders = json.loads(R.get("phase:1:first_senders") or "[]")
    return {"first_senders": first_senders}
