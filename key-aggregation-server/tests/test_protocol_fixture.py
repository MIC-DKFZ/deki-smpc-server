import json
from pathlib import Path

import torch
from deki_smpc.protocol.schema import ModelSchema

from app.domain.topology import derive_tree_plan, tree_plan_hash


def test_server_reads_the_normative_golden_fixture() -> None:
    fixture_path = Path(__file__).resolve().parents[3] / "deki-smpc" / "tests" / "fixtures" / "protocol-v1.json"
    fixture = json.loads(fixture_path.read_text())
    schema = ModelSchema.from_state_dict({"weight": torch.zeros(2)}, precision_bits=8)
    assert schema.hash == fixture["model_schema_hash"]
    assert sum(client["masked"][0] for client in fixture["clients"]) == fixture["aggregate_encoded"][0]


def test_server_reads_the_protocol_11_tree_fixture() -> None:
    fixture_path = Path(__file__).resolve().parents[3] / "deki-smpc" / "tests" / "fixtures" / "protocol-v1.1-tree.json"
    fixture = json.loads(fixture_path.read_text())

    assert fixture["protocol_version"] == "1.1"
    row = {**fixture["context"], "protocol_version": "1.1"}
    plan = derive_tree_plan(row, fixture["ephemeral_manifest"])
    assert plan == fixture["plan"]
    assert tree_plan_hash(plan) == fixture["plan_hash"]
