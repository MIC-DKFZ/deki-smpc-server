from app.config import R
from app.utils import reset_all_state
from fastapi import APIRouter, HTTPException

# Create a router
router = APIRouter()


@router.post("/reset")
async def reset_tasks():
    await reset_all_state()
    return {"message": "All tasks and queues have been reset."}


@router.get("/redis/keys")
async def get_redis_keys():
    return R.keys("phase:*")


@router.get("/tasks")
async def debug_tasks():
    data = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        data[key] = R.lrange(key, 0, -1)
    return data


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


@router.get("/registered-participants")
async def get_registered_participants():
    """
    Endpoint to get the list of registered participants.
    """
    registered_clients = R.smembers("clients:registered")
    if not registered_clients:
        raise HTTPException(status_code=404, detail="No registered clients found.")

    return {"registered_clients": list(registered_clients)}
