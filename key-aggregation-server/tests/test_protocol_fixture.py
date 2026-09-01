import json
from pathlib import Path

import torch
from deki_smpc.protocol.schema import ModelSchema


def test_server_reads_the_normative_golden_fixture() -> None:
    fixture_path = Path(__file__).resolve().parents[3] / "deki-smpc" / "tests" / "fixtures" / "protocol-v1.json"
    fixture = json.loads(fixture_path.read_text())
    schema = ModelSchema.from_state_dict({"weight": torch.zeros(2)}, precision_bits=8)
    assert schema.hash == fixture["model_schema_hash"]
    assert sum(client["masked"][0] for client in fixture["clients"]) == fixture["aggregate_encoded"][0]
