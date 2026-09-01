from __future__ import annotations

import base64
import os
import threading
import time
from collections.abc import Mapping
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path

import torch
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from dynamic_network_architectures.architectures.unet import PlainConvUNet
from fastapi.testclient import TestClient

os.environ.setdefault("DEKI_ADMIN_TOKEN", "test-bootstrap-token-at-least-32-chars")
os.environ.setdefault("DEKI_FEDERATIONS_JSON", "{}")
os.environ.setdefault("DEKI_DATABASE_PATH", "/tmp/deki-test-bootstrap.sqlite")
os.environ.setdefault("DEKI_ARTIFACT_PATH", "/tmp/deki-test-bootstrap-artifacts")

from deki_smpc import FedAvgClient
from deki_smpc.protocol.schema import ModelSchema

from app.config import Settings
from app.main import create_app
from app.persistence.database import Database
from app.storage.filesystem import FilesystemArtifactStore
from app.worker.aggregation import AggregationWorker


def _identity_config():
    private = {f"client-{index}": Ed25519PrivateKey.generate() for index in range(1, 4)}
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
    federation = {"demo": {name: {"token": tokens[name], "signing_public_key": public[name]} for name in private}}
    return private, public, seeds, tokens, federation


def _model(value: float) -> torch.nn.Module:
    model = torch.nn.Linear(2, 1)
    with torch.no_grad():
        model.weight.fill_(value)
        model.bias.fill_(value * 2)
    return model


def _nnunet_model(value: float) -> PlainConvUNet:
    """Build a small real nnU-Net architecture suitable for an integration test."""
    model = PlainConvUNet(
        input_channels=1,
        n_stages=3,
        features_per_stage=(4, 8, 16),
        conv_op=torch.nn.Conv2d,
        kernel_sizes=((3, 3), (3, 3), (3, 3)),
        strides=((1, 1), (2, 2), (2, 2)),
        n_conv_per_stage=(1, 1, 1),
        num_classes=3,
        n_conv_per_stage_decoder=(1, 1),
        conv_bias=True,
        norm_op=None,
        dropout_op=None,
        nonlin=torch.nn.LeakyReLU,
        nonlin_kwargs={"inplace": True},
        deep_supervision=True,
    )
    with torch.no_grad():
        for tensor in model.state_dict().values():
            if tensor.is_floating_point():
                tensor.fill_(value)
    return model


class _StatefulModel(torch.nn.Module):
    def __init__(self, value: float, counter: int) -> None:
        super().__init__()
        self.weight = torch.nn.Parameter(torch.tensor([value]))
        self.register_buffer("counter", torch.tensor(counter, dtype=torch.int64))


def _create_round(operator: TestClient, schema: ModelSchema, participants: list[str], key: str) -> str:
    response = operator.post(
        "/v1/federations/demo/rounds",
        headers={"Authorization": "Bearer admin-secret", "Idempotency-Key": key},
        json={
            "protocol_version": "1.0",
            "model_schema": schema.as_dict(),
            "model_schema_hash": schema.hash,
            "participants": participants,
            "deadline_seconds": 30,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["round_id"]


def _aggregate_client(
    name: str,
    clients: dict[str, FedAvgClient],
    models: dict[str, torch.nn.Module],
    round_id: str,
    timeout: timedelta,
    results: dict[str, dict[str, torch.Tensor]],
    errors: list[Exception],
    tensor_policies: Mapping[str, str] | None = None,
) -> None:
    try:
        results[name] = clients[name].aggregate(
            model=models[name],
            round_id=round_id,
            timeout=timeout,
            tensor_policies=tensor_policies,
        )
    except Exception as exc:  # noqa: BLE001 - the parent thread reports protocol failures
        errors.append(exc)


def test_five_repeated_rounds_reuse_clients_and_survive_api_lifespans(tmp_path: Path) -> None:
    _, public, seeds, tokens, federation = _identity_config()
    settings = Settings(
        tmp_path / "metadata.sqlite", tmp_path / "artifacts", federation, "admin-secret", 2_000_000, 30, 3600, 0.01
    )
    app = create_app(settings)
    models = {name: _model(float(index)) for index, name in enumerate(public, 1)}
    schema = ModelSchema.from_state_dict(next(iter(models.values())).state_dict())
    database = Database(settings.database_path)
    store = FilesystemArtifactStore(settings.artifact_path, settings.max_artifact_bytes)

    with TestClient(app) as operator:
        first_round = _create_round(operator, schema, list(public), "create-0")
        replay = operator.post(
            "/v1/federations/demo/rounds",
            headers={"Authorization": "Bearer admin-secret", "Idempotency-Key": "create-0"},
            json={
                "protocol_version": "1.0",
                "model_schema": schema.as_dict(),
                "model_schema_hash": schema.hash,
                "participants": list(public),
                "deadline_seconds": 30,
            },
        )
        assert (
            replay.status_code == 200
            and replay.json()["round_id"] == first_round
            and replay.json()["idempotent_replay"]
        )
        unauthorized = operator.get(f"/v1/rounds/{first_round}", headers={"Authorization": "Bearer wrong"})
        assert unauthorized.status_code == 401

    # Reopening the API demonstrates that the accepted round exists outside process memory.
    with ExitStack() as stack:
        test_clients = {name: stack.enter_context(TestClient(app)) for name in public}
        clients = {
            name: FedAvgClient(
                base_url="http://testserver",
                federation_id="demo",
                client_id=name,
                auth_token=tokens[name],
                identity_private_key=seeds[name],
                trusted_signing_keys=public,
                allow_insecure_http=True,
                http_client=test_clients[name],
            )
            for name in public
        }
        rounds = [first_round]
        with TestClient(app) as operator:
            rounds.extend(_create_round(operator, schema, list(public), f"create-{index}") for index in range(1, 5))
        worker = AggregationWorker(database, store)
        stopped = threading.Event()
        worker_thread = threading.Thread(target=lambda: _worker_loop(worker, stopped), daemon=True)
        worker_thread.start()
        try:
            for round_id in rounds:
                results: dict[str, dict[str, torch.Tensor]] = {}
                errors: list[Exception] = []
                threads = [
                    threading.Thread(
                        target=_aggregate_client,
                        args=(
                            name,
                            clients,
                            models,
                            round_id,
                            timedelta(seconds=15),
                            results,
                            errors,
                        ),
                    )
                    for name in public
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(20)
                assert not errors
                assert not any(thread.is_alive() for thread in threads)
                for result in results.values():
                    torch.testing.assert_close(result["weight"], torch.full((1, 2), 2.0))
                    torch.testing.assert_close(result["bias"], torch.tensor([4.0]))
                assert database.get_round(round_id)["state"] == "COMPLETED"
        finally:
            stopped.set()
            worker_thread.join(2)


def test_three_clients_aggregate_real_nnunet_end_to_end(tmp_path: Path) -> None:
    """Aggregate the PlainConvUNet architecture used by nnU-Net through the full v1 protocol."""
    _, public, seeds, tokens, federation = _identity_config()
    settings = Settings(
        tmp_path / "metadata.sqlite",
        tmp_path / "artifacts",
        federation,
        "admin-secret",
        8_000_000,
        30,
        3600,
        0.01,
    )
    app = create_app(settings)
    models = {name: _nnunet_model(float(index)) for index, name in enumerate(public, 1)}
    schema = ModelSchema.from_state_dict(next(iter(models.values())).state_dict())

    with TestClient(app) as operator:
        round_id = _create_round(operator, schema, list(public), "nnunet-round")

    database = Database(settings.database_path)
    worker = AggregationWorker(
        database,
        FilesystemArtifactStore(settings.artifact_path, settings.max_artifact_bytes),
    )
    stopped = threading.Event()
    worker_thread = threading.Thread(target=lambda: _worker_loop(worker, stopped), daemon=True)
    worker_thread.start()
    errors: list[Exception] = []
    results: dict[str, dict[str, torch.Tensor]] = {}

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

            threads = [
                threading.Thread(
                    target=_aggregate_client,
                    args=(
                        name,
                        clients,
                        models,
                        round_id,
                        timedelta(seconds=20),
                        results,
                        errors,
                    ),
                )
                for name in public
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)

        assert not errors
        assert not any(thread.is_alive() for thread in threads)
        assert len(results) == 3
        reference_keys = set(next(iter(models.values())).state_dict())
        assert reference_keys
        for result in results.values():
            assert set(result) == reference_keys
            for name, tensor in result.items():
                torch.testing.assert_close(
                    tensor,
                    torch.full_like(tensor, 2.0),
                    msg=lambda message, tensor_name=name: f"{tensor_name}: {message}",
                )
        assert database.get_round(round_id)["state"] == "COMPLETED"
    finally:
        stopped.set()
        worker_thread.join(2)


def test_sum_policy_aggregates_while_integer_buffers_stay_local(tmp_path: Path) -> None:
    _, public, seeds, tokens, federation = _identity_config()
    settings = Settings(
        tmp_path / "metadata.sqlite",
        tmp_path / "artifacts",
        federation,
        "admin-secret",
        2_000_000,
        30,
        3600,
        0.01,
    )
    app = create_app(settings)
    models: dict[str, torch.nn.Module] = {
        name: _StatefulModel(float(index), index * 10) for index, name in enumerate(public, 1)
    }
    policies = {"weight": "SUM"}
    schema = ModelSchema.from_state_dict(next(iter(models.values())).state_dict(), policies=policies)
    with TestClient(app) as operator:
        round_id = _create_round(operator, schema, list(public), "sum-round")

    database = Database(settings.database_path)
    worker = AggregationWorker(
        database,
        FilesystemArtifactStore(settings.artifact_path, settings.max_artifact_bytes),
    )
    stopped = threading.Event()
    worker_thread = threading.Thread(target=lambda: _worker_loop(worker, stopped), daemon=True)
    worker_thread.start()
    results: dict[str, dict[str, torch.Tensor]] = {}
    errors: list[Exception] = []
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
            threads = [
                threading.Thread(
                    target=_aggregate_client,
                    args=(
                        name,
                        clients,
                        models,
                        round_id,
                        timedelta(seconds=15),
                        results,
                        errors,
                        policies,
                    ),
                )
                for name in public
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)

        assert not errors
        assert not any(thread.is_alive() for thread in threads)
        for index, name in enumerate(public, 1):
            torch.testing.assert_close(results[name]["weight"], torch.tensor([6.0]))
            assert results[name]["counter"].item() == index * 10
        assert database.get_round(round_id)["state"] == "COMPLETED"
    finally:
        stopped.set()
        worker_thread.join(2)


def _worker_loop(worker: AggregationWorker, stopped: threading.Event) -> None:
    while not stopped.is_set():
        worker.run_once()
        time.sleep(0.005)


def test_idempotency_conflict_and_upload_limit_cleanup(tmp_path: Path) -> None:
    _, public, _, _, federation = _identity_config()
    settings = Settings(
        tmp_path / "metadata.sqlite", tmp_path / "artifacts", federation, "admin-secret", 32, 30, 3600, 0.01
    )
    app = create_app(settings)
    schema = ModelSchema.from_state_dict(_model(1).state_dict())
    with TestClient(app) as operator:
        _create_round(operator, schema, list(public), "same-key")
        changed = operator.post(
            "/v1/federations/demo/rounds",
            headers={"Authorization": "Bearer admin-secret", "Idempotency-Key": "same-key"},
            json={
                "protocol_version": "1.0",
                "model_schema": {**schema.as_dict(), "precision_bits": 12},
                "model_schema_hash": schema.hash,
                "participants": list(public),
                "deadline_seconds": 30,
            },
        )
        assert changed.status_code in {409, 422}
    store = FilesystemArtifactStore(settings.artifact_path, 3)

    async def chunks():
        yield b"12"
        yield b"34"

    import asyncio

    try:
        asyncio.run(store.put_stream(chunks()))
    except ValueError:
        pass
    assert list(settings.artifact_path.glob(".*.tmp")) == []
