import asyncio
import json
import logging
from threading import Lock
from typing import Dict, List, Optional, Tuple

from config import R, aggregated_state_dict, aggregated_state_dict_lock
from fastapi import HTTPException, Request, Response
from fastapi.responses import Response
from fastapi.responses import Response as FastAPIResponse

logging.basicConfig(level=logging.INFO)


class Task:
    pass  # TODO: Implement the base Task class


class ActiveTasks(Task):
    """
    Manages upload/download tasks for clients.
    Clients can check if they have a task (upload or download).
    A download is only available after upload is completed.
    """

    def __init__(self):
        self.tasks = []
        self.lock = Lock()

    def add_task(self, sender: str, receiver: str, queue: str) -> None:
        """
        Add a new upload/download task between two clients.
        """
        task = {
            "from": sender,
            "to": receiver,
            "upload_done": False,
            "download_done": False,
            "queue": queue,
        }
        with self.lock:
            self.tasks.append(task)
            logging.info(f"Added task: {task}")

    def check_for_task(self, client_name: str) -> dict | None:
        """
        Check if there's any task for this client.
        Returns one of:
        - {'action': 'upload', 'task': {...}}
        - {'action': 'download', 'task': {...}}
        - None (no task for this client)
        """
        with self.lock:
            for task in self.tasks:
                if task["from"] == client_name and not task["upload_done"]:
                    logging.info(f"Upload task found for {client_name}: {task}")
                    return {"action": "upload", "task": task}
                elif (
                    task["to"] == client_name
                    and task["upload_done"]
                    and not task["download_done"]
                ):
                    logging.info(f"Download task found for {client_name}: {task}")
                    return {"action": "download", "task": task}

        logging.info(f"No task found for {client_name}")
        return None

    def complete_task(self, client_name: str) -> dict | None:
        """
        Marks the upload or download as complete, depending on what’s available.
        Removes task if both upload and download are done.
        """
        with self.lock:
            for task in self.tasks:
                # Complete upload
                if task["from"] == client_name and not task["upload_done"]:
                    task["upload_done"] = True
                    logging.info(f"Upload complete: {task}")
                    return task

                # Complete download and remove task
                elif (
                    task["to"] == client_name
                    and task["upload_done"]
                    and not task["download_done"]
                ):
                    task["download_done"] = True
                    logging.info(f"Download complete: {task}")
                    self.tasks.remove(task)
                    return task
        return None

    def get_all_tasks(self) -> dict:
        """
        Returns all current tasks (for inspection/debugging).
        """
        with self.lock:
            return {"active": self.tasks.copy(), "pending": []}

    def clear_tasks(self) -> None:
        """
        Clears all tasks.
        """
        with self.lock:
            self.tasks.clear()
            logging.info("All phase 1 tasks cleared.")


class ActiveTasksPhase2(Task):
    def __init__(self):
        self.tasks = []  # Active tasks
        self.pending_tasks = []  # Waiting on a dependency
        self.dependency_map = {}  # Maps (from -> to) to their parent
        self.lock = Lock()
        self.phase_2_clients = set()  # Clients that are in phase 2
        self.phase_1_groups = {}  # Groups that are in phase 1

    def add_task(
        self, sender: str, receiver: str, queue: str, depends_on: tuple | None = None
    ) -> None:
        """
        Adds a task. If it depends on another task, it's put in pending until the dependency is done.
        """
        task = {
            "from": sender,
            "to": receiver,
            "upload_done": False,
            "download_done": False,
            "queue": queue,
        }

        with self.lock:
            if depends_on:
                self.pending_tasks.append(task)
                self.dependency_map[(sender, receiver)] = depends_on
                logging.info(f"Task pending due to dependency {depends_on}: {task}")
            else:
                self.tasks.append(task)
                logging.info(f"Task added: {task}")

    def check_for_task(self, client_name: str) -> dict | None:
        with self.lock:
            for task in self.tasks:
                if task["from"] == client_name and not task["upload_done"]:
                    logging.info(f"Upload task found for {client_name}: {task}")
                    return {"action": "upload", "task": task}
                elif (
                    task["to"] == client_name
                    and task["upload_done"]
                    and not task["download_done"]
                ):
                    logging.info(f"Download task found for {client_name}: {task}")
                    return {"action": "download", "task": task}

            logging.info(f"No task found for {client_name}")
            return None

    def complete_task(self, client_name: str) -> dict | None:
        with self.lock:
            for task in list(self.tasks):  # Copy to avoid mutation during iteration
                if task["from"] == client_name and not task["upload_done"]:
                    task["upload_done"] = True
                    logging.info(f"Upload complete: {task}")
                    return task

                elif (
                    task["to"] == client_name
                    and task["upload_done"]
                    and not task["download_done"]
                ):
                    task["download_done"] = True
                    logging.info(f"Download complete: {task}")
                    self.tasks.remove(task)
                    self.__activate_dependents(task)
                    return task
        return None

    def __activate_dependents(self, completed_task: dict):
        """
        Check if any pending task can now be activated.
        """
        completed_key = (completed_task["from"], completed_task["to"])
        ready = [
            task
            for task in self.pending_tasks
            if self.dependency_map.get((task["from"], task["to"])) == completed_key
        ]

        for task in ready:
            self.pending_tasks.remove(task)
            self.tasks.append(task)
            logging.info(f"Dependency met. Task activated: {task}")

    def get_all_tasks(self) -> dict:
        with self.lock:
            return {
                "active": self.tasks.copy(),
                "pending": self.pending_tasks.copy(),
            }

    def clear_tasks(self) -> None:
        with self.lock:
            self.tasks.clear()
            self.pending_tasks.clear()
            self.dependency_map.clear()
            logging.info("All phase 2 tasks cleared.")

    def load_tasks_from_json(self, task_data: dict, queue_name: str):
        """
        Parses the transmission task list and adds them to ActiveTasks.
        Determines dependencies automatically.
        """
        raw_tasks: List[str] = task_data.get(queue_name, [])
        edges = []

        # Parse the JSON strings into dicts
        for raw in raw_tasks:
            edge = json.loads(raw)
            edges.append((edge["from"], edge["to"]))

        # Build reverse lookup: who is sending to whom
        to_from_map = {to: frm for frm, to in edges}

        for frm, to in edges:
            depends_on = None

            # If the current sender was previously a recipient, then it depends on the previous task
            if frm in to_from_map:
                depends_on = (to_from_map[frm], frm)

            self.phase_2_clients.add(frm)
            self.phase_2_clients.add(to)

            self.add_task(frm, to, queue_name, depends_on=depends_on)

        # Memorize the last recipient
        if edges:
            self.last_recipient = edges[-1][1]
        else:
            self.last_recipient = self.phase_1_groups[0][0]

    def get_last_recipient(self) -> str | None:
        return self.last_recipient


class FileTransfer:
    def __init__(self):
        self._store: Dict[str, Tuple[bytes, str]] = {}
        self._lock = asyncio.Lock()

    async def upload_artifact(self, model_id: str, request: Request) -> Response:
        buf = bytearray()
        async for chunk in request.stream():
            if chunk:
                buf.extend(chunk)

        content_type = request.headers.get("content-type") or "application/octet-stream"

        data = bytes(buf)
        async with self._lock:
            self._store[model_id] = (data, content_type)

        return Response(status_code=204)

    async def download_artifact(self, model_id: str) -> FastAPIResponse:
        """
        Download bytes for the given model_id.
        404 if the ID does not exist.
        """
        async with self._lock:
            entry: Optional[Tuple[bytes, str]] = self._store.get(model_id)
            if entry is None:
                raise HTTPException(
                    status_code=404, detail="No artifact for this model_id"
                )
            body, ctype = entry

        headers = {
            "Content-Disposition": f'attachment; filename="{model_id}.bin"',
            "Content-Length": str(len(body)),
            "Cache-Control": "no-store",
        }
        return FastAPIResponse(content=body, media_type=ctype, headers=headers)


tasks_phase_1 = ActiveTasks()
tasks_phase_2 = ActiveTasksPhase2()

file_transfer_aggregation = FileTransfer()
file_transfer_fl = FileTransfer()


async def reset_all_state():
    """Internal helper to reset"""
    # Clear all tasks and queues
    R.delete("queue:aggregation:initial")
    R.delete("queue:aggregation:groups")
    tasks_phase_1.clear_tasks()
    tasks_phase_2.clear_tasks()

    # Delete all registered & finished clients
    R.delete("clients:registered")
    R.delete("clients:finished")

    # Reset the phase in Redis
    R.set("phase", 1)

    # Clear the model weights
    for key in sorted(R.keys("phase:*:weights:*")):
        R.delete(key)
    # Clear the final sum
    R.delete("final:sum")

    # Clear the buffers
    async with file_transfer_aggregation._lock:
        file_transfer_aggregation._store.clear()
    async with file_transfer_fl._lock:
        file_transfer_fl._store.clear()

    async with aggregated_state_dict_lock:
        global aggregated_state_dict
        aggregated_state_dict = None

    # Clear the first senders
    R.delete("phase:1:first_senders")
