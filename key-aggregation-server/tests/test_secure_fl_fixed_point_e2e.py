import asyncio
import importlib
import io
import sys
import types
from pathlib import Path

import torch


class _InMemoryTransfer:
    def __init__(self, store: dict[str, tuple[bytes, str]]) -> None:
        self._store = store
        self._lock = asyncio.Lock()


def _serialize(state_dict: dict[str, torch.Tensor]) -> bytes:
    buffer = io.BytesIO()
    torch.save(state_dict, buffer, _use_new_zipfile_serialization=True)
    return buffer.getvalue()


def _load_routes(
    transfer: _InMemoryTransfer,
) -> types.ModuleType:
    server_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(server_root))

    config = types.ModuleType("app.config")
    config.DEVICE = torch.device("cpu")
    config.NUM_CLIENTS = 3
    config.aggregated_state_dict = None
    config.aggregated_state_dict_lock = asyncio.Lock()

    utils = types.ModuleType("app.utils")
    utils.file_transfer_fl = transfer

    sys.modules["app.config"] = config
    sys.modules["app.utils"] = utils
    sys.modules.pop("app.secure_fl.routes", None)
    return importlib.import_module("app.secure_fl.routes")


def test_server_aggregates_24_bit_fixed_point_payload_end_to_end() -> None:
    scale = 2**24
    client_weights = [
        torch.tensor([-0.31415927, 0.00000009, 1.2345679], dtype=torch.float32),
        torch.tensor([0.27182818, -0.00000003, -2.3456788], dtype=torch.float32),
        torch.tensor([0.16180341, 0.00000012, 3.4567890], dtype=torch.float32),
    ]
    encoded = [(weights.double() * scale).round().long() for weights in client_weights]
    payloads = {
        f"fl:client-{index}:weights": (_serialize({"weight": tensor}), "")
        for index, tensor in enumerate(encoded)
    }
    input_payload_size = len(next(iter(payloads.values()))[0])
    transfer = _InMemoryTransfer(payloads)
    routes = _load_routes(transfer)

    asyncio.run(routes.aggregate_models_if_ready())
    response = asyncio.run(routes.retrieve_model())
    aggregated = torch.load(
        io.BytesIO(response.body), map_location="cpu", weights_only=True
    )["weight"]
    decoded_average = (aggregated.double() / scale).float() / len(client_weights)
    expected_average = torch.stack(client_weights).mean(dim=0)

    assert torch.equal(aggregated, sum(encoded[1:], encoded[0].clone()))
    assert aggregated.dtype == torch.int64
    assert aggregated.shape == client_weights[0].shape
    assert (
        aggregated.untyped_storage().nbytes() == encoded[0].untyped_storage().nbytes()
    )
    assert len(response.body) == input_payload_size
    torch.testing.assert_close(decoded_average, expected_average, rtol=0, atol=2**-25)
    assert transfer._store == {}
