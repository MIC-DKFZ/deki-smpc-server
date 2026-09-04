"""Idempotent safetensors aggregation worker."""

from __future__ import annotations

import hashlib
import json
import signal
import sqlite3
import time
from pathlib import Path

import torch
from safetensors.torch import load_file, save

from app.config import Settings
from app.persistence.database import Database
from app.storage.filesystem import FilesystemArtifactStore

FIELD_PRIME = 2**127 - 1


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class AggregationWorker:
    def __init__(self, database: Database, store: FilesystemArtifactStore, lease_seconds: float = 900.0) -> None:
        self.database = database
        self.store = store
        self.lease_seconds = lease_seconds

    def run_once(self) -> bool:
        job = self.database.claim_job(self.lease_seconds)
        if not job:
            return False
        round_id, job_id, claim_token = str(job["round_id"]), str(job["job_id"]), str(job["claim_token"])
        try:
            round_row, artifacts = self.database.job_inputs(round_id)
            if len(artifacts) != int(str(round_row["expected_count"])):
                raise ValueError("submission count changed after barrier")
            schema = json.loads(str(round_row["model_schema_json"]))
            expected = {entry["name"]: entry for entry in schema["entries"] if entry["policy"] in {"MEAN", "SUM"}}
            aggregate: dict[str, torch.Tensor] | None = None
            aggregate_tag = 0
            for artifact in artifacts:
                self.database.renew_job(job_id, claim_token, self.lease_seconds)
                path = str(artifact["path"])
                digest = _file_sha256(path)
                if digest != artifact["digest"]:
                    raise ValueError("artifact content digest changed")
                tensors = load_file(path, device="cpu")
                if set(tensors) != set(expected):
                    raise ValueError("artifact tensor names do not match schema")
                for name, tensor in tensors.items():
                    if tensor.dtype != torch.int64 or list(tensor.shape) != expected[name]["shape"]:
                        raise ValueError("artifact tensor shape or dtype does not match schema")
                if aggregate is None:
                    aggregate = {name: tensor.clone() for name, tensor in tensors.items()}
                else:
                    for name in sorted(aggregate):
                        aggregate[name] = aggregate[name] + tensors[name]
                tag = int(str(artifact["integrity_tag"]), 16)
                if not 0 <= tag < FIELD_PRIME:
                    raise ValueError("integrity tag outside field")
                aggregate_tag = (aggregate_tag + tag) % FIELD_PRIME
            if aggregate is None:
                raise ValueError("aggregation job has no inputs")
            # Under protocol 1.1 this sum is intentionally still masked by the
            # independently aggregated model key.  The worker never receives
            # the group-encrypted final key needed to clear it.
            self.database.renew_job(job_id, claim_token, self.lease_seconds)
            data = save({name: aggregate[name].contiguous() for name in sorted(aggregate)})
            stored = self.store.put_bytes(data)
            try:
                self.database.complete_job(
                    job_id,
                    round_id,
                    claim_token,
                    stored,
                    aggregate_tag.to_bytes(16, "big").hex(),
                )
            except Exception:
                self.store.delete(stored.path)
                raise
        except (OSError, sqlite3.OperationalError) as exc:
            self.database.fail_job(job_id, round_id, claim_token, str(exc), retryable=True)
        except Exception as exc:  # noqa: BLE001 - unexpected input failures must durably fail the job
            self.database.fail_job(job_id, round_id, claim_token, str(exc), retryable=False)
        return True

    def run_maintenance(self) -> int:
        """Remove terminal rounds whose configured retention window elapsed."""
        self.database.expire_rounds()
        purged = 0
        for round_id, paths in self.database.retention_candidates():
            for path in paths:
                self.store.delete(path)
            self.database.purge_round(round_id)
            purged += 1
        return purged


def main() -> None:
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize(settings.federation_config)
    worker = AggregationWorker(
        database,
        FilesystemArtifactStore(settings.artifact_path, settings.max_artifact_bytes),
        settings.worker_lease_seconds,
    )
    next_maintenance = 0.0
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while not stopping:
        now = time.monotonic()
        if now >= next_maintenance:
            worker.run_maintenance()
            next_maintenance = now + settings.maintenance_interval_seconds
        if not worker.run_once():
            time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":
    main()
