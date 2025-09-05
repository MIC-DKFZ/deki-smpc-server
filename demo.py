import httpx
import requests
from openfhe import *
from tqdm import tqdm
import io
import torch
import torch.nn as nn
import torchvision.models as models
import struct

CHUNK_SIZE = 1024 * 1024  # 1MB

_MAGIC = b"CHNK"  # 4 bytes
_VERSION = 1  # 1 byte
_HDR_FMT = ">4sB"  # magic, version
_CNT_FMT = ">I"  # uint32: number of chunks
_LEN_FMT = ">Q"  # uint64: per-chunk length


def pack_chunks(chunks):
    """
    Pack an iterable of bytes-like objects into a single bytes payload.

    Wire format:
      [MAGIC=CHNK][VERSION=1][COUNT:u32][ (LEN:u64)(DATA) ... repeated COUNT times ]
    """
    # Normalize to bytes and validate types early
    bchunks = []
    for idx, ch in enumerate(chunks):
        if not isinstance(ch, (bytes, bytearray, memoryview)):
            raise TypeError(f"Chunk {idx} is not bytes-like")
        bchunks.append(bytes(ch))

    parts = []
    parts.append(struct.pack(_HDR_FMT, _MAGIC, _VERSION))
    parts.append(struct.pack(_CNT_FMT, len(bchunks)))
    for ch in bchunks:
        parts.append(struct.pack(_LEN_FMT, len(ch)))
        parts.append(ch)
    return b"".join(parts)


def unpack_chunks(blob):
    """
    Unpack a bytes payload produced by pack_chunks back into a list of bytes.
    Raises ValueError if the blob is malformed.
    """
    mv = memoryview(blob)
    offset = 0

    # Header
    if len(mv) < struct.calcsize(_HDR_FMT) + struct.calcsize(_CNT_FMT):
        raise ValueError("Blob too small")

    magic, version = struct.unpack_from(_HDR_FMT, mv, offset)
    offset += struct.calcsize(_HDR_FMT)

    if magic != _MAGIC:
        raise ValueError("Bad magic header")
    if version != _VERSION:
        raise ValueError(f"Unsupported version {version}")

    (count,) = struct.unpack_from(_CNT_FMT, mv, offset)
    offset += struct.calcsize(_CNT_FMT)

    chunks = []
    for i in range(count):
        if offset + struct.calcsize(_LEN_FMT) > len(mv):
            raise ValueError(f"Truncated before chunk {i} length")
        (length,) = struct.unpack_from(_LEN_FMT, mv, offset)
        offset += struct.calcsize(_LEN_FMT)

        end = offset + length
        if end > len(mv):
            raise ValueError(f"Truncated in chunk {i} data")
        chunks.append(bytes(mv[offset:end]))
        offset = end

    if offset != len(mv):
        raise ValueError("Trailing bytes after last chunk")

    return chunks


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


def _iter_bytes(b: bytes, pbar: tqdm):
    mv = memoryview(b)
    total = len(b)
    i = 0
    while i < total:
        j = i + CHUNK_SIZE
        chunk = mv[i:j]
        pbar.update(len(chunk))
        yield chunk
        i = j


def _stream_upload(url: str, headers: dict, content_iterable):
    # httpx >= 0.28 streaming upload path
    with httpx.Client(timeout=None) as client:
        with client.stream(
            "PUT", url, headers=headers, content=content_iterable
        ) as resp:
            resp.raise_for_status()


def automatic_rescale_demo(scal_tech):
    if scal_tech == ScalingTechnique.FLEXIBLEAUTO:
        print("\n\n\n ===== FlexibleAutoDemo =============\n")
    else:
        print("\n\n\n ===== FixedAutoDemo =============\n")

    # batch_size = 0
    parameters = CCParamsCKKSRNS()
    parameters.SetMultiplicativeDepth(5)
    parameters.SetScalingModSize(50)
    parameters.SetScalingTechnique(scal_tech)
    # parameters.SetBatchSize(batch_size)
    # parameters.SetSlots(8192)

    cc = GenCryptoContext(parameters)

    print(f"CKKS scheme is using ring dimension {cc.GetRingDimension()}\n")
    print(f"CKKS scheme is using batch size {cc.GetBatchSize()}\n")

    cc.Enable(PKESchemeFeature.PKE)
    cc.Enable(PKESchemeFeature.KEYSWITCH)
    cc.Enable(PKESchemeFeature.LEVELEDSHE)

    # Generate the public/private key pair

    url = "http://localhost:8080/key-distribution/generate-keys"
    preshared_secret = "my_secure_presHared_secret_123!"

    payload = {"preshared_secret": preshared_secret}

    response = requests.post(url, json=payload)

    # Download the public key

    url = "http://localhost:8080/key-distribution/download/public_key"
    response = requests.get(url, json=payload)

    with open("public.key", "wb") as f:
        f.write(response.content)

    pubkey, _ = DeserializePublicKey("public.key", BINARY)

    # Encode and encrypt a vector of values

    model = models.resnet18(weights=None)  # random init, or pretrained if you want
    print(model.conv1.weight[0][0])
    state_dict = model.state_dict()

    # Example ignore list: ignore running stats from BatchNorm
    ignore_keys = [k for k in state_dict.keys() if "running" in k]

    x, mapping_dict, ignored_dict = flatten_state_dict(state_dict, ignore_keys)

    max_length = cc.GetRingDimension() // 2

    print(f"Max length: {max_length}")

    chunks = chunk_list(x, max_length=max_length)

    for i, chunk in enumerate(chunks):
        ptxt = cc.MakeCKKSPackedPlaintext(chunk)
        c = cc.Encrypt(pubkey, ptxt)
        # Serialize and deserialize the ciphertext
        SerializeToFile("ciphertext.txt", c, BINARY)
        # load ciphertext
        with open("ciphertext.txt", "rb") as f:
            chunks[i] = f.read()

    del c
    del cc

    payload = pack_chunks(chunks)
    clients = 2

    for client in range(clients):
        url = "http://localhost:8081/secure-fl/upload"

        headers = {
            "Content-Type": "application/octet-stream",
            "X-Client-Name": f"{client+1}",
            "X-Chunk-Index": str(i),
            "X-Chunk-Total": str(len(chunks)),
        }
        with tqdm(
            total=len(payload),
            unit="B",
            unit_scale=True,
            desc=f"Uploading: ciphertext.txt for client {client+1}",
            ascii=True,
        ) as pbar:
            _stream_upload(url, headers, _iter_bytes(payload, pbar))

    del payload

    # Download the secret key

    payload = {"preshared_secret": preshared_secret}

    url = "http://localhost:8080/key-distribution/download/secret_key"
    response = requests.get(url, json=payload)

    with open("secret.key", "wb") as f:
        f.write(response.content)

    seckey, _ = DeserializePrivateKey("secret.key", BINARY)

    # Download the aggregated ciphertext

    url = "http://localhost:8081/secure-fl/download-aggregate"

    headers = {
        "Content-Type": "application/octet-stream",
        "X-Client-Name": "1",
    }

    with httpx.Client(timeout=None) as client:
        with client.stream("GET", url, headers=headers) as resp:

            total = int(resp.headers.get("content-length") or 0)
            buf = io.BytesIO()

            with tqdm(
                total=total if total > 0 else None,
                unit="B",
                unit_scale=True,
                desc=f"Downloading: aggregated ciphertext",
                ascii=True,
            ) as pbar:
                for chunk in resp.iter_bytes(CHUNK_SIZE):
                    if chunk:
                        buf.write(chunk)
                        pbar.update(len(chunk))

    buf.seek(0)

    chunks = unpack_chunks(buf.read())

    for i, chunk in enumerate(chunks):
        # write to file for deserialization
        with open("ciphertext_aggregated.txt", "wb") as f:
            f.write(chunk)

        chunk, _ = DeserializeCiphertext("ciphertext_aggregated.txt", BINARY)
        cc = chunk.GetCryptoContext()
        cc.EvalMultKeyGen(seckey)
        # Decrypt each chunk
        chunk = cc.Decrypt(chunk, seckey)
        chunks[i] = chunk.GetCKKSPackedValue()
        # keep only the real part
        for j, number in enumerate(chunks[i]):
            chunks[i][j] = number.real

    del cc
    del chunk

    # Rebuild the state_dict
    reconstructed_state_dict = reconstruct_state_dict(
        unchunk_list(chunks), mapping_dict, ignored_dict
    )

    # Load it back into a model
    model.load_state_dict(reconstructed_state_dict)
    print(model.conv1.weight[0][0])
    # result.SetLength(batch_size)
    # print(f"Result: {result}")


def main():
    if get_native_int() != 128:
        automatic_rescale_demo(ScalingTechnique.FLEXIBLEAUTO)


if __name__ == "__main__":
    main()
