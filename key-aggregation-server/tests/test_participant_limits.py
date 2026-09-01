import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain.models import CreateRoundRequest, KeyBundleArtifact
from app.persistence.database import canonical_json


def _round_body(participant_count: int) -> dict[str, object]:
    schema = {
        "aggregation_policy": "EQUAL_WEIGHTED",
        "entries": [{"name": "weight", "shape": [1], "dtype": "float32", "policy": "MEAN"}],
        "precision_bits": 16,
    }
    return {
        "protocol_version": "1.0",
        "model_schema": schema,
        "model_schema_hash": hashlib.sha256(canonical_json(schema).encode()).hexdigest(),
        "participants": [f"site-{index}" for index in range(participant_count)],
    }


def test_protocol_model_does_not_impose_a_participant_maximum() -> None:
    request = CreateRoundRequest.model_validate(_round_body(65))

    assert len(request.participants) == 65


def test_key_bundle_model_does_not_impose_a_peer_maximum() -> None:
    bundle = KeyBundleArtifact.model_validate(
        {
            "messages": {f"site-{index}": {"nonce": "A" * 16, "ciphertext": "A"} for index in range(64)},
            "signature": "A" * 80,
        }
    )

    assert len(bundle.messages) == 64


def test_configured_participant_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEKI_ADMIN_TOKEN", "admin-token-with-at-least-32-characters")
    from app.main import create_app

    settings = Settings(
        tmp_path / "metadata.sqlite",
        tmp_path / "artifacts",
        {},
        "admin-token-with-at-least-32-characters",
        max_participants=3,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/v1/federations/demo/rounds",
            headers={
                "Authorization": "Bearer admin-token-with-at-least-32-characters",
                "Idempotency-Key": "configured-participant-limit",
            },
            json=_round_body(4),
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PARTICIPANT_LIMIT_EXCEEDED"


def test_participant_limit_environment_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEKI_ADMIN_TOKEN", "admin-token-with-at-least-32-characters")
    monkeypatch.setenv("DEKI_MAX_PARTICIPANTS", "128")

    assert Settings.from_env().max_participants == 128


def test_participant_limit_environment_setting_must_allow_protocol_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEKI_ADMIN_TOKEN", "admin-token-with-at-least-32-characters")
    monkeypatch.setenv("DEKI_MAX_PARTICIPANTS", "2")

    with pytest.raises(RuntimeError, match="must be at least 3"):
        Settings.from_env()
