"""Strict v1 request models."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_TENSORS = 4096
MAX_RANK = 8
MAX_DIMENSION = 1_000_000_000
RESERVED_PREFIX = "__deki_"
SUPPORTED_DTYPES = Literal[
    "float16",
    "bfloat16",
    "float32",
    "float64",
    "uint8",
    "int8",
    "int16",
    "int32",
    "int64",
]
TensorDimension = Annotated[int, Field(ge=0, le=MAX_DIMENSION)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TensorSchema(StrictModel):
    name: str = Field(min_length=1, max_length=1024)
    shape: list[TensorDimension] = Field(max_length=MAX_RANK)
    dtype: SUPPORTED_DTYPES
    policy: Literal["MEAN", "SUM", "KEEP_LOCAL"]

    @model_validator(mode="after")
    def validate_tensor(self) -> "TensorSchema":
        if self.name.startswith(RESERVED_PREFIX) or "\x00" in self.name:
            raise ValueError("tensor name is reserved or malformed")
        if self.policy in {"MEAN", "SUM"} and not self.dtype.startswith(("float", "bfloat")):
            raise ValueError("MEAN and SUM require a floating tensor")
        return self


class ModelSchema(StrictModel):
    aggregation_policy: Literal["EQUAL_WEIGHTED"]
    entries: list[TensorSchema] = Field(min_length=1, max_length=MAX_TENSORS)
    precision_bits: int = Field(ge=0, le=52)

    @model_validator(mode="after")
    def validate_entries(self) -> "ModelSchema":
        names = [entry.name for entry in self.entries]
        if names != sorted(names) or len(set(names)) != len(names):
            raise ValueError("tensor entries must have unique names in canonical order")
        if not any(entry.policy in {"MEAN", "SUM"} for entry in self.entries):
            raise ValueError("schema must contain an uploaded tensor")
        return self


class CreateRoundRequest(StrictModel):
    protocol_version: str = "1.0"
    model_schema: ModelSchema
    model_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    participants: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]] = Field(min_length=3)
    deadline_seconds: int | None = Field(default=None, ge=5, le=86400)


class PublicKeyArtifact(StrictModel):
    public_key: str = Field(min_length=40, max_length=64)
    signature: str = Field(min_length=80, max_length=128)


class KeyEnvelope(StrictModel):
    nonce: str = Field(min_length=16, max_length=24)
    ciphertext: str = Field(min_length=1, max_length=4096)


class KeyBundleArtifact(StrictModel):
    messages: dict[str, KeyEnvelope] = Field(min_length=2)
    signature: str = Field(min_length=80, max_length=128)


class KeyCompleteRequest(StrictModel):
    context_commitment: str = Field(pattern=r"^[0-9a-f]{64}$")


class AbortRequest(StrictModel):
    reason: str = Field(default="OPERATOR_ABORT", min_length=1, max_length=128)
