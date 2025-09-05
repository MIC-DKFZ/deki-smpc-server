import torch
import torch.nn as nn
import torchvision.models as models


def flatten_state_dict(
    state_dict: dict[str, torch.Tensor], ignore_keys: list[str] = None
):
    if ignore_keys is None:
        ignore_keys = []

    flat_list = []
    mapping_dict = {}
    ignored_dict = {}

    offset = 0
    for key, tensor in state_dict.items():
        if key in ignore_keys:
            ignored_dict[key] = tensor.clone()
            continue

        values = tensor.flatten().tolist()
        n = len(values)

        mapping_dict[key] = {
            "shape": tensor.shape,
            "start": offset,
            "end": offset + n,
        }

        flat_list.extend(values)
        offset += n

    return flat_list, mapping_dict, ignored_dict


def reconstruct_state_dict(
    flat_list: list[float],
    mapping_dict: dict[str, dict],
    ignored_dict: dict[str, torch.Tensor],
) -> dict[str, torch.Tensor]:
    new_state_dict = {}

    for key, meta in mapping_dict.items():
        start, end = meta["start"], meta["end"]
        shape = meta["shape"]

        tensor_values = flat_list[start:end]
        tensor = torch.tensor(tensor_values, dtype=torch.float32).reshape(shape)

        new_state_dict[key] = tensor

    # restore ignored values
    for key, tensor in ignored_dict.items():
        new_state_dict[key] = tensor.clone()

    return new_state_dict


def chunk_list(flat_list, max_length=8192):
    """
    Splits a long list into chunks of size max_length.

    Args:
        flat_list (list[float]): the input list
        max_length (int): maximum length of each chunk

    Returns:
        list[list[float]]: list of chunks
    """
    return [flat_list[i : i + max_length] for i in range(0, len(flat_list), max_length)]


def unchunk_list(chunks):
    """
    Flattens a list of lists back into a single list.

    Args:
        chunks (list[list[float]]): list of chunks

    Returns:
        list[float]: flattened list
    """
    return [x for chunk in chunks for x in chunk]


# --- Example with resnet18 ---
if __name__ == "__main__":
    model = models.resnet18(weights=None)  # random init, or pretrained if you want
    print(model.conv1.weight[0][0])
    state_dict = model.state_dict()

    # Example ignore list: ignore running stats from BatchNorm
    ignore_keys = [k for k in state_dict.keys() if "running" in k]

    flat_list, mapping_dict, ignored_dict = flatten_state_dict(state_dict, ignore_keys)

    chunks = chunk_list(flat_list)

    # Rebuild the state_dict
    reconstructed_state_dict = reconstruct_state_dict(
        unchunk_list(chunks), mapping_dict, ignored_dict
    )

    # Load it back into a model
    model.load_state_dict(reconstructed_state_dict)
    print(model.conv1.weight[0][0])
    print("Reconstruction successful ✅")
