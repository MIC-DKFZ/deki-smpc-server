"""Test-only worker that modifies one aggregate coordinate before commit."""

from safetensors.torch import load, save

from app.config import Settings
from app.persistence.database import Database
from app.storage.filesystem import FilesystemArtifactStore
from app.worker.aggregation import AggregationWorker


class TamperingStore(FilesystemArtifactStore):
    def put_bytes(self, data: bytes):
        tensors = load(data)
        first = min(tensors)
        tensors[first].view(-1)[0] ^= 1
        return super().put_bytes(save(tensors))


settings = Settings.from_env()
database = Database(settings.database_path)
database.initialize(settings.federation_config)
worker = AggregationWorker(database, TamperingStore(settings.artifact_path, settings.max_artifact_bytes))
if __name__ == "__main__":
    import signal
    import time

    stopping = False

    def request_stop(_signum, _frame):
        global stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stopping:
        if not worker.run_once():
            time.sleep(settings.worker_poll_seconds)
