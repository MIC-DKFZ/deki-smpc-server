from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

os.environ.setdefault("DEKI_ADMIN_TOKEN", "test-bootstrap-token-at-least-32-chars")
os.environ.setdefault("DEKI_FEDERATIONS_JSON", "{}")
os.environ.setdefault("DEKI_DATABASE_PATH", "/tmp/deki-v11-state-bootstrap.sqlite")
os.environ.setdefault("DEKI_ARTIFACT_PATH", "/tmp/deki-v11-state-bootstrap-artifacts")

from app.config import Settings
from app.main import create_app
from app.persistence.database import Database, canonical_json
from app.storage.filesystem import StoredObject


def _tree_round(tmp_path: Path, count: int = 7) -> tuple[Database, str, dict[str, str], dict[str, object]]:
    identities = {f"client-{index}": Ed25519PrivateKey.generate() for index in range(count)}
    public = {
        name: base64.b64encode(
            key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        ).decode()
        for name, key in identities.items()
    }
    tokens = {name: f"independent-high-entropy-token-{name}" for name in identities}
    federation: dict[str, object] = {
        "demo": {name: {"token": tokens[name], "signing_public_key": public[name]} for name in identities}
    }
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize(federation)  # type: ignore[arg-type]
    schema: dict[str, object] = {
        "aggregation_policy": "EQUAL_WEIGHTED",
        "entries": [{"name": "weight", "shape": [1], "dtype": "float32", "policy": "MEAN"}],
        "precision_bits": 16,
    }
    schema_hash = hashlib.sha256(canonical_json(schema).encode()).hexdigest()
    response, _ = database.create_round(
        "demo",
        "1.1",
        schema,
        schema_hash,
        list(identities),
        "a" * 64,
        60,
        3600,
        "operator",
        "create",
        "request",
    )
    round_id = str(response["round_id"])
    for index, name in enumerate(identities):
        database.record_json_slot(
            round_id,
            name,
            "public_key",
            {"public_key": base64.b64encode(bytes([index]) * 32).decode(), "signature": "A" * 88},
            f"public-{name}",
            f"digest-{name}",
        )
    for name in identities:
        database.key_complete(round_id, name, "b" * 64, f"complete-{name}", f"complete-digest-{name}")
    return database, round_id, tokens, federation


def test_task_graph_survives_restart_and_activates_dependencies_atomically(tmp_path: Path) -> None:
    database, round_id, _, _ = _tree_round(tmp_path)
    plan_record = database.tree_plan(round_id)
    assert plan_record is not None
    tasks = plan_record["plan"]["tasks"]
    initial = [task for task in tasks if not task["dependencies"]]
    assert {database.next_key_action(round_id, task["sender"])["task_id"] for task in initial} == {
        task["task_id"] for task in initial
    }

    for index, task in enumerate(tasks):
        # Reopen the repository at every edge to model API process restarts in
        # group, binary-reduction, and carry stages.
        database = Database(tmp_path / "metadata.sqlite")
        digest = f"{index + 1:064x}"
        stored = StoredObject(f"artifact-{index}", str(tmp_path / f"artifact-{index}"), f"{index:064x}", 32)
        database.record_task_artifact(
            round_id,
            task["sender"],
            task["task_id"],
            stored,
            digest,
            "A" * 16,
            "A" * 88,
            f"upload-{index}",
            f"upload-digest-{index}",
        )
        database.acknowledge_task(
            round_id,
            task["sender"],
            task["task_id"],
            digest,
            f"ack-{index}",
            f"ack-digest-{index}",
        )

    action = database.next_key_action(round_id, plan_record["plan"]["root_client_id"])
    assert action is not None and action["action"] == "PUBLISH_FINAL"
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM task_receipts WHERE round_id=?", (round_id,)).fetchone()[
            0
        ] == len(tasks)


def test_wrong_tree_actor_durably_fails_round(tmp_path: Path) -> None:
    database, round_id, tokens, federation = _tree_round(tmp_path, 3)
    plan_record = database.tree_plan(round_id)
    assert plan_record is not None
    task = plan_record["plan"]["tasks"][0]
    wrong_actor = next(name for name in tokens if name != task["sender"])
    settings = Settings(
        tmp_path / "metadata.sqlite", tmp_path / "artifacts", federation, "admin-secret", 2_000_000, 60, 3600, 0.01
    )

    with TestClient(create_app(settings)) as client:
        response = client.put(
            f"/v1/rounds/{round_id}/key-tasks/{task['task_id']}/artifact",
            headers={
                "Authorization": f"Bearer {tokens[wrong_actor]}",
                "Idempotency-Key": "wrong-actor",
                "X-Artifact-Metadata": base64.b64encode(b"{}").decode(),
                "X-Artifact-Signature": "A" * 88,
            },
            content=b"not-an-artifact",
        )

    assert response.status_code == 422
    row = database.get_round(round_id)
    assert row is not None
    assert row["state"] == "FAILED"
    assert row["failure_code"] == "KEY_ARTIFACT_REJECTED"


def test_modified_stored_tree_artifact_is_rejected_and_fails_round(tmp_path: Path) -> None:
    database, round_id, tokens, federation = _tree_round(tmp_path, 3)
    plan_record = database.tree_plan(round_id)
    assert plan_record is not None
    task = plan_record["plan"]["tasks"][0]
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    path = artifact_root / "manual-artifact"
    original = b"ciphertext-and-gcm-tag"
    path.write_bytes(original)
    stored = StoredObject("manual-artifact", str(path), hashlib.sha256(original).hexdigest(), len(original))
    ciphertext_digest = "c" * 64
    database.record_task_artifact(
        round_id,
        task["sender"],
        task["task_id"],
        stored,
        ciphertext_digest,
        "A" * 16,
        "A" * 88,
        "manual-upload",
        "manual-request",
    )
    database.acknowledge_task(
        round_id,
        task["sender"],
        task["task_id"],
        ciphertext_digest,
        "manual-ack",
        "manual-ack-request",
    )
    path.write_bytes(b"X" + original[1:])
    settings = Settings(
        tmp_path / "metadata.sqlite", artifact_root, federation, "admin-secret", 2_000_000, 60, 3600, 0.01
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            f"/v1/rounds/{round_id}/key-tasks/{task['task_id']}/artifact",
            headers={"Authorization": f"Bearer {tokens[task['receiver']]}"},
        )

    assert response.status_code == 422
    row = database.get_round(round_id)
    assert row is not None and row["state"] == "FAILED"


def test_repeatable_migration_preserves_active_protocol_10_round(tmp_path: Path) -> None:
    database = Database(tmp_path / "metadata.sqlite")
    database.initialize({})
    schema: dict[str, object] = {
        "aggregation_policy": "EQUAL_WEIGHTED",
        "entries": [{"name": "weight", "shape": [1], "dtype": "float32", "policy": "MEAN"}],
        "precision_bits": 16,
    }
    response, _ = database.create_round(
        "legacy",
        "1.0",
        schema,
        hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
        ["a", "b", "c"],
        "a" * 64,
        60,
        3600,
        "operator",
        "legacy-create",
        "legacy-request",
    )
    round_id = str(response["round_id"])
    with database.transaction() as connection:
        for table in (
            "final_key_receipts",
            "final_keys",
            "task_receipts",
            "task_dependencies",
            "key_tasks",
            "tree_plans",
        ):
            connection.execute(f"DROP TABLE {table}")

    Database(tmp_path / "metadata.sqlite").initialize({})
    row = database.get_round(round_id)
    assert row is not None
    assert row["protocol_version"] == "1.0"
    assert row["state"] == "REGISTRATION_OPEN"


def test_protocol_11_key_aggregation_expires_after_dropout(tmp_path: Path) -> None:
    database, round_id, _, _ = _tree_round(tmp_path, 5)
    with database.transaction() as connection:
        connection.execute("UPDATE rounds SET deadline_at=? WHERE round_id=?", (time.time() - 1, round_id))

    assert database.expire_rounds() == 1
    row = database.get_round(round_id)
    assert row is not None
    assert row["state"] == "EXPIRED"
    with pytest.raises(ValueError, match="ROUND_CONFLICT"):
        database.next_key_action(round_id, "client-0")
