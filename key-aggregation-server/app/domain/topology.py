"""Canonical protocol-1.1 topology derivation.

This intentionally mirrors ``deki_smpc.protocol.tree``.  Keeping the server
implementation independent is what makes client-side plan verification useful.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _domain(row: dict[str, object], purpose: str) -> bytes:
    fields = (
        purpose,
        row["protocol_version"],
        row["federation_id"],
        row["round_id"],
        row["model_schema_hash"],
        row["participant_manifest_hash"],
        row["aggregation_policy"],
        str(row["precision_bits"]),
    )
    return ("\x00".join(str(value) for value in fields)).encode()


def _shuffle(participants: list[str], seed: bytes) -> list[str]:
    shuffled = sorted(participants)
    counter = 0
    for upper in range(len(shuffled) - 1, 0, -1):
        modulus = upper + 1
        limit = 1 << 256
        cutoff = limit - (limit % modulus)
        while True:
            value = int.from_bytes(hashlib.sha256(seed + counter.to_bytes(8, "big")).digest(), "big")
            counter += 1
            if value < cutoff:
                break
        selected = value % modulus
        shuffled[upper], shuffled[selected] = shuffled[selected], shuffled[upper]
    return shuffled


def _group_sizes(participant_count: int) -> list[int]:
    if participant_count < 3:
        raise ValueError("protocol 1.1 requires at least three participants")
    if participant_count == 5:
        return [5]
    group_count = math.ceil(participant_count / 4)
    base, remainder = divmod(participant_count, group_count)
    sizes = [base + (index < remainder) for index in range(group_count)]
    if any(size not in {3, 4} for size in sizes):
        raise ValueError("could not form balanced groups")
    return sizes


def _walk_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    return [node] + [descendant for child in node["children"] for descendant in _walk_nodes(child)]


def derive_tree_plan(row: dict[str, object], manifest: list[dict[str, str]]) -> dict[str, Any]:
    participants = [record["client_id"] for record in manifest]
    if len(set(participants)) != len(participants):
        raise ValueError("ephemeral manifest contains duplicate participants")
    seed = hashlib.sha256(_domain(row, "tree-topology") + canonical_json(manifest).encode()).digest()
    shuffled = _shuffle(participants, seed)
    sizes = _group_sizes(len(shuffled))
    groups: list[dict[str, Any]] = []
    offset = 0
    for index, size in enumerate(sizes):
        members = shuffled[offset : offset + size]
        offset += size
        groups.append({"group_id": index, "members": members, "coordinator": members[0]})

    active: list[dict[str, Any]] = [
        {"operator": group["coordinator"], "group": group["group_id"], "children": [], "level": -1} for group in groups
    ]
    tree_nodes: list[dict[str, Any]] = []
    level = 0
    while len(active) > 1:
        following: list[dict[str, Any]] = []
        for index in range(0, len(active), 2):
            children = active[index : index + 2]
            node = {
                "operator": children[0]["operator"],
                "children": children,
                "level": level,
                "action": "TREE_COMBINE" if len(children) == 2 else "TREE_CARRY",
                "index": index // 2,
            }
            tree_nodes.append(node)
            following.append(node)
        active = following
        level += 1
    root = active[0]

    def assign_targets(node: dict[str, Any], target: str) -> None:
        node["target"] = target
        for child in node["children"]:
            assign_targets(child, str(node["operator"]))

    assign_targets(root, str(root["operator"]))
    tasks: list[dict[str, Any]] = []
    for group in groups:
        group_id = int(group["group_id"])
        members = list(group["members"])
        previous = f"g{group_id}-start"
        tasks.append(
            {
                "task_id": previous,
                "action": "GROUP_START",
                "stage": "GROUP",
                "level": 0,
                "sender": members[0],
                "receiver": members[1],
                "dependencies": [],
            }
        )
        for position in range(1, len(members)):
            task_id = f"g{group_id}-add-{position}"
            tasks.append(
                {
                    "task_id": task_id,
                    "action": "GROUP_ADD",
                    "stage": "GROUP",
                    "level": 0,
                    "sender": members[position],
                    "receiver": members[position + 1] if position + 1 < len(members) else members[0],
                    "dependencies": [previous],
                }
            )
            previous = task_id
        leaf = next(node for node in _walk_nodes(root) if node.get("group") == group_id)
        task_id = f"g{group_id}-unblind"
        tasks.append(
            {
                "task_id": task_id,
                "action": "GROUP_UNBLIND",
                "stage": "GROUP",
                "level": 0,
                "sender": members[0],
                "receiver": leaf["target"],
                "dependencies": [previous],
            }
        )
        leaf["task_id"] = task_id
    for node in sorted(tree_nodes, key=lambda item: (int(item["level"]), int(item["index"]))):
        task_id = f"t{node['level']}-{node['index']}"
        tasks.append(
            {
                "task_id": task_id,
                "action": node["action"],
                "stage": "TREE",
                "level": int(node["level"]),
                "sender": node["operator"],
                "receiver": node["target"],
                "dependencies": [str(child["task_id"]) for child in node["children"]],
            }
        )
        node["task_id"] = task_id
    return {
        "protocol_version": "1.1",
        "participants": shuffled,
        "groups": groups,
        "tasks": tasks,
        "root_client_id": root["operator"],
        "root_task_id": root["task_id"],
        "tree_levels": math.ceil(math.log2(len(groups))) if len(groups) > 1 else 0,
    }


def tree_plan_hash(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(plan).encode()).hexdigest()
