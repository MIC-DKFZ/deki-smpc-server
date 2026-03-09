from app.config import R
from app.utils import reset_all_state
from fastapi import APIRouter, HTTPException

# Create a router
router = APIRouter()


@router.post("/reset")
async def reset_tasks() -> dict[str, str]:
    """Reset queues, task state, registrations, and cached artifacts."""
    await reset_all_state()
    return {"message": "All tasks and queues have been reset."}


@router.get("/redis/keys")
async def get_redis_keys() -> list[str]:
    """List phase-related Redis keys for quick diagnostics."""
    return R.keys("phase:*")


@router.get("/tasks")
async def debug_tasks() -> dict[str, list[str]]:
    """Return all aggregation queue entries keyed by queue name."""
    data = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        data[key] = R.lrange(key, 0, -1)
    return data


@router.get("/redis/queues")
async def get_redis_queues() -> dict[str, list[str]]:
    """Return all aggregation queue contents from Redis."""
    queues = {}
    for key in sorted(R.keys("queue:aggregation:*")):
        queues[key] = R.lrange(key, 0, -1)
    return queues


@router.get("/redis/queues/{queue_name}")
async def get_redis_queue(queue_name: str) -> dict[str, list[str]]:
    """Return queue contents for a specific aggregation queue."""
    queue = R.lrange(f"queue:aggregation:{queue_name}", 0, -1)
    if not queue:
        raise HTTPException(status_code=404, detail="Queue not found")
    return {queue_name: queue}


@router.get("/registered-participants")
async def get_registered_participants() -> dict[str, list[str]]:
    """Return all currently registered client names."""
    registered_clients = R.smembers("clients:registered")
    if not registered_clients:
        raise HTTPException(status_code=404, detail="No registered clients found.")

    return {"registered_clients": list(registered_clients)}
