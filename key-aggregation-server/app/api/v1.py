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
from app.domain.models import AbortRequest, CreateRoundRequest, KeyBundleArtifact, KeyCompleteRequest, PublicKeyArtifact
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
    if body.protocol_version != "1.0":
        raise api_error(422, "PROTOCOL_VERSION_UNSUPPORTED", "only protocol 1.0 is supported")
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
