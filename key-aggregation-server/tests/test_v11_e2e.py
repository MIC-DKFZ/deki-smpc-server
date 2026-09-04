from __future__ import annotations

import base64
import os
import threading
import time
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import pytest
import torch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from safetensors.torch import load_file

os.environ.setdefault("DEKI_ADMIN_TOKEN", "test-bootstrap-token-at-least-32-chars")
os.environ.setdefault("DEKI_FEDERATIONS_JSON", "{}")
os.environ.setdefault("DEKI_DATABASE_PATH", "/tmp/deki-v11-bootstrap.sqlite")
os.environ.setdefault("DEKI_ARTIFACT_PATH", "/tmp/deki-v11-bootstrap-artifacts")

from deki_smpc import FedAvgClient
from deki_smpc.protocol.errors import ArtifactValidationError
from deki_smpc.protocol.schema import ModelSchema
from deki_smpc.utils import FixedPointConverter

from app.config import Settings
from app.main import create_app
from app.persistence.database import Database
from app.storage.filesystem import FilesystemArtifactStore
from app.worker.aggregation import AggregationWorker


def _configuration(count: int) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, object]]:
    private = {f"client-{index}": Ed25519PrivateKey.generate() for index in range(count)}
    public = {
        name: base64.b64encode(
            key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ).decode()
        for name, key in private.items()
    }
    seeds = {
        name: base64.b64encode(
            key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        ).decode()
        for name, key in private.items()
    }
    tokens = {name: f"independent-high-entropy-token-{name}" for name in private}
    federation: dict[str, object] = {
        "demo": {name: {"token": tokens[name], "signing_public_key": public[name]} for name in private}
    }
    return public, seeds, tokens, federation


def _model(value: float) -> torch.nn.Module:
    model = torch.nn.Linear(2, 1)
    model.register_buffer("local_counter", torch.tensor(int(value * 10), dtype=torch.int64))
    with torch.no_grad():
        model.weight.fill_(value)
        model.bias.fill_(value * 2)
    return model


def _worker_loop(worker: AggregationWorker, stopped: threading.Event) -> None:
    while not stopped.is_set():
        worker.run_once()
        time.sleep(0.002)


@pytest.mark.parametrize("count", [3, 5, 7, 12])
def test_protocol_11_hides_server_result_and_preserves_tensor_policies(tmp_path: Path, count: int) -> None:
    public, seeds, tokens, federation = _configuration(count)
    settings = Settings(
        tmp_path / "metadata.sqlite", tmp_path / "artifacts", federation, "admin-secret", 2_000_000, 30, 3600, 0.005
    )
    app = create_app(settings)
    models = {name: _model(float(index + 1)) for index, name in enumerate(public)}
    policies = {"weight": "SUM"} if count == 5 else None
    schema = ModelSchema.from_state_dict(next(iter(models.values())).state_dict(), policies=policies)
    with TestClient(app) as operator:
        response = operator.post(
            "/v1/federations/demo/rounds",
            headers={"Authorization": "Bearer admin-secret", "Idempotency-Key": f"create-{count}"},
            json={
                "model_schema": schema.as_dict(),
                "model_schema_hash": schema.hash,
                "participants": list(public),
                "deadline_seconds": 30,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["protocol_version"] == "1.1"
        round_id = response.json()["round_id"]

    database = Database(settings.database_path)
    worker = AggregationWorker(database, FilesystemArtifactStore(settings.artifact_path, settings.max_artifact_bytes))
    stopped = threading.Event()
    worker_thread = threading.Thread(target=_worker_loop, args=(worker, stopped), daemon=True)
    worker_thread.start()
    results: dict[str, dict[str, torch.Tensor]] = {}
    errors: list[Exception] = []
    prepared_handles: dict[str, object] = {}
    try:
        with ExitStack() as stack:
            clients = {
                name: FedAvgClient(
                    base_url="http://testserver",
                    federation_id="demo",
                    client_id=name,
                    auth_token=tokens[name],
                    identity_private_key=seeds[name],
                    trusted_signing_keys=public,
                    allow_insecure_http=True,
                    http_client=stack.enter_context(TestClient(app)),
                )
                for name in public
            }

            def aggregate(name: str) -> None:
                try:
                    prepared = None
                    if count == 3:
                        prepared = clients[name].prepare_round(
                            model=models[name],
                            round_id=round_id,
                            timeout=timedelta(seconds=25),
                            tensor_policies=policies,
                        )
                        prepared_handles[name] = prepared
                    results[name] = clients[name].aggregate(
                        model=models[name],
                        round_id=round_id,
                        timeout=timedelta(seconds=25),
                        tensor_policies=policies,
                        prepared_round=prepared,
                    )
                except Exception as exc:  # noqa: BLE001 - reported by the parent test thread
                    errors.append(exc)

            threads = [threading.Thread(target=aggregate, args=(name,)) for name in public]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)

        assert not errors
        assert not any(thread.is_alive() for thread in threads)
        if count == 3:
            first = next(iter(public))
            with pytest.raises(ArtifactValidationError, match="already consumed"):
                clients[first].aggregate(
                    model=models[first],
                    round_id=round_id,
                    prepared_round=prepared_handles[first],  # type: ignore[arg-type]
                )
        mean = (count + 1) / 2
        expected_weight = mean * count if policies else mean
        for name, result in results.items():
            torch.testing.assert_close(result["weight"], torch.full((1, 2), expected_weight))
            torch.testing.assert_close(result["bias"], torch.tensor([mean * 2]))
            torch.testing.assert_close(result["local_counter"], models[name].state_dict()["local_counter"])
        result_record = database.result(round_id)
        assert result_record is not None
        artifact, _ = result_record
        server_result = load_file(str(artifact["path"]))
        assert "local_counter" not in server_result
        converter = FixedPointConverter(schema.precision_bits, max_aggregation_terms=count)
        clear_weight_sum = converter.encode(torch.full((1, 2), mean * count))
        assert not torch.equal(server_result["weight"], clear_weight_sum)
        assert database.get_round(round_id)["state"] == "COMPLETED"
    finally:
        stopped.set()
        worker_thread.join(2)
