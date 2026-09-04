"""Authenticated, round-scoped protocol v1 API."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from typing import Annotated, Any, NoReturn, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from safetensors import safe_open

from app.domain.errors import api_error
from app.domain.models import (
    AbortRequest,
    ArtifactReceipt,
    CreateRoundRequest,
    FinalKeyReceipt,
    KeyBundleArtifact,
    KeyCompleteRequest,
    PublicKeyArtifact,
)
from app.domain.states import RoundState
from app.persistence.database import Database, canonical_json
from app.storage.filesystem import FilesystemArtifactStore

router = APIRouter(prefix="/v1")
FIELD_PRIME = 2**127 - 1


def _database(request: Request) -> Database:
    return cast(Database, request.app.state.database)


def _store(request: Request) -> FilesystemArtifactStore:
    return cast(FilesystemArtifactStore, request.app.state.artifact_store)


def _bearer(request: Request) -> str:
    value = request.headers.get("Authorization", "")
    if not value.startswith("Bearer ") or len(value) <= 7:
        raise api_error(401, "AUTHENTICATION_FAILED", "valid bearer authentication is required")
    return value[7:]


def participant_identity(request: Request) -> dict[str, object]:
    member = _database(request).member_for_token(_bearer(request))
    if not member:
        raise api_error(401, "AUTHENTICATION_FAILED", "credential is invalid or revoked")
    return member


def operator_identity(request: Request) -> str:
    token = _bearer(request)
    if not hmac.compare_digest(token, request.app.state.settings.admin_token):
        raise api_error(403, "AUTHORIZATION_FAILED", "operator authorization is required")
    return "operator"


def _idem(value: Annotated[str | None, Header(alias="Idempotency-Key")]) -> str:
    if not value or len(value) > 128:
        raise api_error(400, "IDEMPOTENCY_KEY_REQUIRED", "a bounded Idempotency-Key is required")
    return value


def _round_for_member(database: Database, round_id: str, member: dict[str, object]) -> dict[str, object]:
    round_row = database.get_round(round_id)
    if not round_row:
        raise api_error(404, "ROUND_NOT_FOUND", "round does not exist", round_id)
    if round_row["federation_id"] != member["federation_id"] or not database.participant(
        round_id, str(member["client_id"])
    ):
        raise api_error(403, "AUTHORIZATION_FAILED", "caller is not a round participant", round_id)
    return round_row


def _domain(row: dict[str, object], client_id: str, purpose: str) -> bytes:
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
    return ("\x00".join(str(v) for v in fields)).encode()


def _verify_signature(
    row: dict[str, object], member: dict[str, object], purpose: str, value: object, signature: str
) -> None:
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(str(member["signing_public_key"]), validate=True))
        key.verify(
            base64.b64decode(signature, validate=True),
            _domain(row, str(member["client_id"]), purpose) + canonical_json(value).encode(),
        )
    except Exception as exc:
        raise api_error(422, "ARTIFACT_INVALID", "identity signature is invalid", str(row["round_id"])) from exc


def _translate(exc: ValueError, round_id: str | None = None) -> NoReturn:
    code = str(exc)
    mapping = {
        "IDEMPOTENCY_CONFLICT": (409, code, "idempotency key was reused with different content"),
        "ROUND_CONFLICT": (409, code, "operation is not allowed in the current round state"),
        "ROUND_NOT_FOUND": (404, code, "round does not exist"),
        "NOT_READY": (409, "ROUND_CONFLICT", "round barrier is not complete"),
        "KEY_CONTEXT_MISMATCH": (422, "ROUND_FAILED", "clients committed different key contexts"),
        "KEY_BUNDLE_INVALID": (422, "ARTIFACT_INVALID", "key bundle is incomplete"),
    }
    status, error_code, message = mapping.get(code, (422, "ARTIFACT_INVALID", "artifact validation failed"))
    raise api_error(status, error_code, message, round_id)


@router.post("/federations/{federation_id}/rounds")
def create_round(
    federation_id: str,
    body: CreateRoundRequest,
    request: Request,
    _: Annotated[str, Depends(operator_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    if body.protocol_version not in {"1.0", "1.1"}:
        raise api_error(422, "PROTOCOL_VERSION_UNSUPPORTED", "supported protocols are 1.0 and 1.1")
    max_participants = request.app.state.settings.max_participants
    if max_participants is not None and len(body.participants) > max_participants:
        raise api_error(
            422,
            "PARTICIPANT_LIMIT_EXCEEDED",
            f"round exceeds the configured limit of {max_participants} participants",
        )
    if len(set(body.participants)) != len(body.participants):
        raise api_error(422, "ARTIFACT_INVALID", "participant IDs must be unique")
    schema = body.model_schema.model_dump(mode="json")
    uploaded_elements = sum(
        math.prod(entry.shape) for entry in body.model_schema.entries if entry.policy in {"MEAN", "SUM"}
    )
    if uploaded_elements > request.app.state.settings.max_artifact_bytes // 8:
        raise api_error(422, "ARTIFACT_INVALID", "encoded model exceeds the configured artifact limit")
    schema_digest = hashlib.sha256(canonical_json(schema).encode()).hexdigest()
    if schema_digest != body.model_schema_hash:
        raise api_error(422, "SCHEMA_MISMATCH", "schema hash does not match canonical schema")
    manifest = []
    for client_id in sorted(body.participants):
        member = database.member(federation_id, client_id)
        if not member or member["role"] != "participant":
            raise api_error(422, "UNKNOWN_CLIENT", "participant is not an enrolled federation member")
        manifest.append({"client_id": client_id, "signing_public_key": member["signing_public_key"]})
    manifest_hash = hashlib.sha256(canonical_json(manifest).encode()).hexdigest()
    request_digest = hashlib.sha256(canonical_json(body.model_dump(mode="json")).encode()).hexdigest()
    try:
        response, replay = database.create_round(
            federation_id,
            body.protocol_version,
            schema,
            body.model_schema_hash,
            sorted(body.participants),
            manifest_hash,
            body.deadline_seconds or request.app.state.settings.round_ttl_seconds,
            request.app.state.settings.retention_seconds,
            "operator",
            idem_key,
            request_digest,
        )
    except ValueError as exc:
        _translate(exc)
    response["idempotent_replay"] = replay
    return response


@router.get("/rounds/{round_id}")
def get_round(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> dict[str, object]:
    row = _round_for_member(_database(request), round_id, member)
    return {
        "round_id": row["round_id"],
        "federation_id": row["federation_id"],
        "state": row["state"],
        "state_version": row["state_version"],
        "protocol_version": row["protocol_version"],
        "model_schema_hash": row["model_schema_hash"],
        "participant_manifest_hash": row["participant_manifest_hash"],
        "expected_participant_count": row["expected_count"],
        "aggregation_policy": row["aggregation_policy"],
        "precision_bits": row["precision_bits"],
        "deadline_at": row["deadline_at"],
        "failure_code": row["failure_code"],
        "failure_detail": row["failure_detail"],
        "retry_after": 1,
    }


@router.get("/rounds/{round_id}/participants/me")
def get_me(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> dict[str, object]:
    _round_for_member(_database(request), round_id, member)
    participant = _database(request).participant(round_id, str(member["client_id"]))
    if participant is None:
        raise api_error(403, "AUTHORIZATION_FAILED", "caller is not a round participant", round_id)
    return {key: participant[key] for key in ("client_id", "state", "completed", "last_seen")}


@router.put("/rounds/{round_id}/artifacts/public_key")
def put_public_key(
    round_id: str,
    body: PublicKeyArtifact,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    row = _round_for_member(database, round_id, member)
    value = {"public_key": body.public_key}
    try:
        raw = base64.b64decode(body.public_key, validate=True)
        if len(raw) != 32:
            raise ValueError
    except Exception as exc:
        raise api_error(422, "ARTIFACT_INVALID", "X25519 public key is invalid", round_id) from exc
    _verify_signature(row, member, "ephemeral-public-key", value, body.signature)
    payload = body.model_dump(mode="json")
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    try:
        response, replay = database.record_json_slot(
            round_id, str(member["client_id"]), "public_key", payload, idem_key, digest
        )
    except ValueError as exc:
        _translate(exc, round_id)
    response["idempotent_replay"] = replay
    return response


@router.get("/rounds/{round_id}/artifacts/public_keys")
def get_public_keys(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> dict[str, object]:
    database = _database(request)
    _round_for_member(database, round_id, member)
    try:
        return {"public_keys": database.public_keys(round_id)}
    except ValueError as exc:
        _translate(exc, round_id)


@router.put("/rounds/{round_id}/artifacts/key_bundle")
def put_key_bundle(
    round_id: str,
    body: KeyBundleArtifact,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    row = _round_for_member(database, round_id, member)
    expected = set(json.loads(str(row["participants_json"]))) - {str(member["client_id"])}
    if set(body.messages) != expected:
        raise api_error(422, "ARTIFACT_INVALID", "key bundle recipients do not match the committed manifest", round_id)
    payload = body.model_dump(mode="json")
    value = {"messages": payload["messages"]}
    _verify_signature(row, member, "key-bundle", value, body.signature)
    try:
        for envelope in body.messages.values():
            if (
                len(base64.b64decode(envelope.nonce, validate=True)) != 12
                or len(base64.b64decode(envelope.ciphertext, validate=True)) > 4096
            ):
                raise ValueError
    except Exception as exc:
        raise api_error(422, "ARTIFACT_INVALID", "key envelope is malformed", round_id) from exc
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    try:
        response, replay = database.record_json_slot(
            round_id, str(member["client_id"]), "key_bundle", payload, idem_key, digest
        )
    except ValueError as exc:
        _translate(exc, round_id)
    response["idempotent_replay"] = replay
    return response


@router.get("/rounds/{round_id}/artifacts/key_bundle")
def get_key_bundles(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> Response:
    database = _database(request)
    _round_for_member(database, round_id, member)
    try:
        bundles = database.incoming_bundles(round_id, str(member["client_id"]))
    except ValueError as exc:
        _translate(exc, round_id)
    if bundles is None:
        return Response(status_code=204, headers={"Retry-After": "1"})
    return JSONResponse({"bundles": bundles})


@router.post("/rounds/{round_id}/key-setup/complete")
def key_setup_complete(
    round_id: str,
    body: KeyCompleteRequest,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    _round_for_member(database, round_id, member)
    payload = body.model_dump(mode="json")
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    try:
        return database.key_complete(round_id, str(member["client_id"]), body.context_commitment, idem_key, digest)
    except ValueError as exc:
        _translate(exc, round_id)


def _artifact_value(
    task: dict[str, object], plan_hash: str, schema_hash: str, nonce: str, digest: str, size: int
) -> dict[str, object]:
    return {
        "task_id": task["task_id"],
        "action": task["action"],
        "stage": task["stage"],
        "level": task["level"],
        "sender": task["sender"],
        "receiver": task["receiver"],
        "dependencies": task["dependencies"],
        "plan_hash": plan_hash,
        "model_schema_hash": schema_hash,
        "nonce": nonce,
        "ciphertext_digest": digest,
        "ciphertext_size": size,
    }


def _final_key_value(
    row: dict[str, object], plan_hash: str, root: str, nonce: str, digest: str, size: int
) -> dict[str, object]:
    return {
        "task_id": "final-key",
        "stage": "FINAL_DISTRIBUTION",
        "level": -1,
        "sender": root,
        "receiver": "ALL_PARTICIPANTS",
        "dependencies": [],
        "round_id": row["round_id"],
        "plan_hash": plan_hash,
        "model_schema_hash": row["model_schema_hash"],
        "nonce": nonce,
        "ciphertext_digest": digest,
        "ciphertext_size": size,
    }


def _decode_metadata(encoded: str | None, round_id: str) -> dict[str, object]:
    try:
        if encoded is None:
            raise ValueError
        value = json.loads(base64.b64decode(encoded, validate=True))
        if not isinstance(value, dict):
            raise TypeError
        return cast(dict[str, object], value)
    except Exception as exc:
        raise api_error(422, "ARTIFACT_INVALID", "encrypted artifact metadata is malformed", round_id) from exc


def _ciphertext_digest(path: str, nonce: bytes) -> str:
    digest = hashlib.sha256(nonce)
    trailing = b""
    size = 0
    with open(path, "rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            size += len(chunk)
            combined = trailing + chunk
            if len(combined) > 16:
                digest.update(combined[:-16])
                trailing = combined[-16:]
            else:
                trailing = combined
    if size < 16:
        raise ValueError("ciphertext is too short")
    return digest.hexdigest()


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as artifact:
        while chunk := artifact.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fail_tree(database: Database, round_id: str, client_id: str, message: str) -> NoReturn:
    database.fail_round(round_id, client_id, "KEY_ARTIFACT_REJECTED", message)
    raise api_error(422, "ROUND_FAILED", "encrypted key artifact was rejected", round_id)


@router.get("/rounds/{round_id}/tree-plan")
def get_tree_plan(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> dict[str, object]:
    database = _database(request)
    row = _round_for_member(database, round_id, member)
    if row["protocol_version"] != "1.1":
        raise api_error(409, "ROUND_CONFLICT", "tree plans are only available for protocol 1.1", round_id)
    plan = database.tree_plan(round_id)
    if not plan:
        raise api_error(409, "ROUND_CONFLICT", "tree plan is not ready", round_id)
    return plan


@router.get("/rounds/{round_id}/key-actions/next")
def get_next_key_action(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> Response:
    database = _database(request)
    _round_for_member(database, round_id, member)
    try:
        action = database.next_key_action(round_id, str(member["client_id"]))
    except ValueError as exc:
        _translate(exc, round_id)
    if action is None:
        return Response(status_code=204, headers={"Retry-After": "1"})
    return JSONResponse(action)


@router.put("/rounds/{round_id}/key-tasks/{task_id}/artifact")
async def put_key_task_artifact(
    round_id: str,
    task_id: str,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
    encoded_metadata: Annotated[str | None, Header(alias="X-Artifact-Metadata")] = None,
    signature: Annotated[str | None, Header(alias="X-Artifact-Signature")] = None,
) -> dict[str, object]:
    database, store = _database(request), _store(request)
    row = _round_for_member(database, round_id, member)
    client_id = str(member["client_id"])
    try:
        metadata = _decode_metadata(encoded_metadata, round_id)
    except Exception:  # noqa: BLE001 - malformed metadata is a protocol failure
        _fail_tree(database, round_id, client_id, "tree artifact metadata is malformed")
    plan_record = database.tree_plan(round_id)
    if row["protocol_version"] != "1.1" or not plan_record:
        _fail_tree(database, round_id, client_id, "tree task is unavailable")
    plan = cast(dict[str, Any], plan_record["plan"])
    task = next((item for item in plan["tasks"] if item["task_id"] == task_id), None)
    if task is None or task["sender"] != client_id or signature is None:
        _fail_tree(database, round_id, client_id, "tree task actor or identifier is invalid")
    stored = None
    try:
        stored = await store.put_stream(request.stream())
        nonce = base64.b64decode(str(metadata.get("nonce")), validate=True)
        ciphertext_digest = _ciphertext_digest(stored.path, nonce)
        expected = _artifact_value(
            task,
            str(plan_record["plan_hash"]),
            str(row["model_schema_hash"]),
            str(metadata.get("nonce")),
            ciphertext_digest,
            stored.size,
        )
        if len(nonce) != 12 or metadata != expected:
            raise ValueError("artifact metadata differs from the authorized task")
        _verify_signature(row, member, "tree-artifact", expected, signature)
        request_digest = hashlib.sha256(
            canonical_json({"metadata": expected, "signature": signature, "content": stored.digest}).encode()
        ).hexdigest()
        response, replay = database.record_task_artifact(
            round_id,
            client_id,
            task_id,
            stored,
            ciphertext_digest,
            str(metadata["nonce"]),
            signature,
            idem_key,
            request_digest,
        )
    except Exception as exc:  # noqa: BLE001 - rejected encrypted tasks durably fail the round
        if stored is not None:
            store.delete(stored.path)
        _fail_tree(database, round_id, client_id, str(exc))
    if replay:
        store.delete(stored.path)
    response["idempotent_replay"] = replay
    return response


@router.get("/rounds/{round_id}/key-tasks/{task_id}/artifact")
def get_key_task_artifact(
    round_id: str,
    task_id: str,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
) -> Response:
    database = _database(request)
    _round_for_member(database, round_id, member)
    try:
        artifact, metadata = database.task_artifact(round_id, task_id, str(member["client_id"]))
    except ValueError as exc:
        _translate(exc, round_id)
    try:
        stored_digest = _file_digest(str(artifact["path"]))
    except OSError:
        _fail_tree(database, round_id, str(member["client_id"]), "stored tree artifact is unavailable")
    if not hmac.compare_digest(stored_digest, str(artifact["digest"])):
        _fail_tree(database, round_id, str(member["client_id"]), "stored tree artifact digest changed")
    signature = str(metadata.pop("signature"))
    encoded = base64.b64encode(canonical_json(metadata).encode()).decode()
    return FileResponse(
        str(artifact["path"]),
        media_type="application/octet-stream",
        headers={
            "X-Artifact-Metadata": encoded,
            "X-Artifact-Signature": signature,
            "X-Content-SHA256": str(artifact["digest"]),
            "Cache-Control": "no-store",
        },
    )


@router.post("/rounds/{round_id}/key-tasks/{task_id}/ack")
def acknowledge_key_task(
    round_id: str,
    task_id: str,
    body: ArtifactReceipt,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    _round_for_member(database, round_id, member)
    payload = body.model_dump(mode="json")
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    try:
        return database.acknowledge_task(
            round_id, str(member["client_id"]), task_id, body.artifact_digest, idem_key, digest
        )
    except ValueError as exc:
        _fail_tree(database, round_id, str(member["client_id"]), str(exc))


@router.put("/rounds/{round_id}/final-key")
async def put_final_key(
    round_id: str,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
    encoded_metadata: Annotated[str | None, Header(alias="X-Artifact-Metadata")] = None,
    signature: Annotated[str | None, Header(alias="X-Artifact-Signature")] = None,
) -> dict[str, object]:
    database, store = _database(request), _store(request)
    row = _round_for_member(database, round_id, member)
    client_id = str(member["client_id"])
    try:
        metadata = _decode_metadata(encoded_metadata, round_id)
    except Exception:  # noqa: BLE001 - malformed metadata is a protocol failure
        _fail_tree(database, round_id, client_id, "final key metadata is malformed")
    plan_record = database.tree_plan(round_id)
    if not plan_record or signature is None:
        _fail_tree(database, round_id, client_id, "final key metadata is unavailable")
    root = str(cast(dict[str, Any], plan_record["plan"])["root_client_id"])
    if client_id != root:
        _fail_tree(database, round_id, client_id, "only the tree root may publish the final key")
    stored = None
    try:
        stored = await store.put_stream(request.stream())
        nonce = base64.b64decode(str(metadata.get("nonce")), validate=True)
        ciphertext_digest = _ciphertext_digest(stored.path, nonce)
        expected = _final_key_value(
            row,
            str(plan_record["plan_hash"]),
            root,
            str(metadata.get("nonce")),
            ciphertext_digest,
            stored.size,
        )
        if len(nonce) != 12 or metadata != expected:
            raise ValueError("final key metadata differs from the authorized action")
        _verify_signature(row, member, "final-key", expected, signature)
        request_digest = hashlib.sha256(
            canonical_json({"metadata": expected, "signature": signature, "content": stored.digest}).encode()
        ).hexdigest()
        response, replay = database.record_final_key(
            round_id,
            client_id,
            stored,
            ciphertext_digest,
            str(metadata["nonce"]),
            signature,
            idem_key,
            request_digest,
        )
    except Exception as exc:  # noqa: BLE001 - rejected final keys durably fail the round
        if stored is not None:
            store.delete(stored.path)
        _fail_tree(database, round_id, client_id, str(exc))
    if replay:
        store.delete(stored.path)
    response["idempotent_replay"] = replay
    return response


@router.get("/rounds/{round_id}/final-key")
def get_final_key(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> Response:
    database = _database(request)
    row = _round_for_member(database, round_id, member)
    final = database.final_key(round_id)
    if not final:
        raise api_error(409, "ROUND_CONFLICT", "final key is not ready", round_id)
    artifact, metadata = final
    try:
        stored_digest = _file_digest(str(artifact["path"]))
    except OSError:
        _fail_tree(database, round_id, str(member["client_id"]), "stored final key is unavailable")
    if not hmac.compare_digest(stored_digest, str(artifact["digest"])):
        _fail_tree(database, round_id, str(member["client_id"]), "stored final key digest changed")
    value = _final_key_value(
        row,
        str(metadata["plan_hash"]),
        str(metadata["sender"]),
        str(metadata["nonce"]),
        str(metadata["ciphertext_digest"]),
        int(str(metadata["ciphertext_size"])),
    )
    encoded = base64.b64encode(canonical_json(value).encode()).decode()
    return FileResponse(
        str(artifact["path"]),
        media_type="application/octet-stream",
        headers={
            "X-Artifact-Metadata": encoded,
            "X-Artifact-Signature": str(metadata["signature"]),
            "X-Content-SHA256": str(artifact["digest"]),
            "Cache-Control": "no-store",
        },
    )


@router.post("/rounds/{round_id}/final-key/ack")
def acknowledge_final_key(
    round_id: str,
    body: FinalKeyReceipt,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    _round_for_member(database, round_id, member)
    payload = body.model_dump(mode="json")
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    try:
        return database.acknowledge_final_key(
            round_id, str(member["client_id"]), body.artifact_digest, idem_key, digest
        )
    except ValueError as exc:
        _fail_tree(database, round_id, str(member["client_id"]), str(exc))


def _validate_safetensors(path: str, schema: dict[str, Any]) -> None:
    expected = {entry["name"]: entry for entry in schema["entries"] if entry["policy"] in {"MEAN", "SUM"}}
    try:
        with safe_open(path, framework="pt", device="cpu") as artifact:  # type: ignore[no-untyped-call]
            if set(artifact.keys()) != set(expected):
                raise ValueError
            for name, entry in expected.items():
                tensor = artifact.get_tensor(name)
                if str(tensor.dtype) != "torch.int64" or list(tensor.shape) != entry["shape"]:
                    raise ValueError
    except Exception as exc:
        raise ValueError("invalid safetensors/schema") from exc


@router.put("/rounds/{round_id}/artifacts/update")
async def put_update(
    round_id: str,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
    integrity_tag: Annotated[str | None, Header(alias="X-Integrity-Tag")] = None,
    schema_hash: Annotated[str | None, Header(alias="X-Model-Schema-Hash")] = None,
) -> dict[str, object]:
    database, store = _database(request), _store(request)
    row = _round_for_member(database, round_id, member)
    if schema_hash != row["model_schema_hash"]:
        raise api_error(422, "SCHEMA_MISMATCH", "update schema hash does not match the round", round_id)
    try:
        if integrity_tag is None or len(bytes.fromhex(integrity_tag)) != 16 or int(integrity_tag, 16) >= FIELD_PRIME:
            raise ValueError
    except ValueError as exc:
        raise api_error(422, "ARTIFACT_INVALID", "integrity tag is malformed", round_id) from exc
    stored = None
    try:
        stored = await store.put_stream(request.stream())
        _validate_safetensors(
            stored.path,
            cast(dict[str, Any], json.loads(str(row["model_schema_json"]))),
        )
        request_digest = hashlib.sha256((stored.digest + integrity_tag).encode()).hexdigest()
        response, replay = database.record_update(
            round_id, str(member["client_id"]), stored, integrity_tag, idem_key, request_digest
        )
    except ValueError as exc:
        if stored is not None:
            store.delete(stored.path)
        _translate(exc, round_id)
    except Exception:
        if stored is not None:
            store.delete(stored.path)
        raise
    if replay:
        store.delete(stored.path)
    response["idempotent_replay"] = replay
    return response


@router.get("/rounds/{round_id}/artifacts/result")
def get_result(
    round_id: str, request: Request, member: Annotated[dict[str, object], Depends(participant_identity)]
) -> Response:
    database = _database(request)
    row = _round_for_member(database, round_id, member)
    if row["state"] not in {RoundState.RESULT_READY.value, RoundState.COMPLETED.value}:
        raise api_error(409, "ROUND_CONFLICT", "result is not ready", round_id)
    result = database.result(round_id)
    if not result:
        raise api_error(503, "RESULT_NOT_READY", "result metadata is not committed", round_id)
    artifact, tag = result
    return FileResponse(
        str(artifact["path"]),
        media_type="application/vnd.safetensors",
        headers={"X-Integrity-Tag": tag, "X-Content-SHA256": str(artifact["digest"]), "Cache-Control": "no-store"},
    )


@router.post("/rounds/{round_id}/complete")
def complete(
    round_id: str,
    request: Request,
    member: Annotated[dict[str, object], Depends(participant_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    database = _database(request)
    _round_for_member(database, round_id, member)
    try:
        return database.complete_participant(
            round_id, str(member["client_id"]), idem_key, hashlib.sha256(b"{}").hexdigest()
        )
    except ValueError as exc:
        _translate(exc, round_id)


@router.post("/rounds/{round_id}/abort")
def abort(
    round_id: str,
    body: AbortRequest,
    request: Request,
    _: Annotated[str, Depends(operator_identity)],
    idem_key: Annotated[str, Depends(_idem)],
) -> dict[str, object]:
    digest = hashlib.sha256(canonical_json(body.model_dump(mode="json")).encode()).hexdigest()
    try:
        response, replay = _database(request).abort(round_id, "operator", body.reason, idem_key, digest)
    except ValueError as exc:
        _translate(exc, round_id)
    response["idempotent_replay"] = replay
    return response
