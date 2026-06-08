import hashlib
import math
from typing import Annotated, Literal
from uuid import UUID

from evidencedesk.identity.crypto import canonical_json
from pydantic import BaseModel, ConfigDict, Field, model_validator

Finite = Annotated[float, Field(allow_inf_nan=False)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SparseVector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indices: list[Annotated[int, Field(ge=0, le=2**32 - 1)]]
    values: list[Annotated[float, Field(gt=0, allow_inf_nan=False)]]

    @model_validator(mode="after")
    def valid(self) -> SparseVector:
        if len(self.indices) != len(self.values) or self.indices != sorted(set(self.indices)):
            raise ValueError("Sparse vector indices must be ordered, unique and match values")
        # Empty BM25 is valid for stopword-only passages; dense retrieval still applies.
        return self


class VectorPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    ordinal: int = Field(ge=0)
    text_sha256: Hash
    dense: list[Finite] = Field(min_length=384, max_length=384)
    sparse: SparseVector

    @model_validator(mode="after")
    def unit_dense(self) -> VectorPoint:
        norm = math.sqrt(sum(x * x for x in self.dense))
        if abs(norm - 1) > 1e-3:
            raise ValueError("Dense vector must have unit norm")
        return self


class VectorArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    workspace_id: UUID
    kb_id: UUID
    document_id: UUID
    revision_id: UUID
    profile_id: str
    profile_hash: Hash
    blocks_sha256: Hash
    points: list[VectorPoint] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def ordering(self) -> VectorArtifact:
        if [p.ordinal for p in self.points] != list(range(len(self.points))):
            raise ValueError("Vector ordinal ordering is inconsistent")
        if len({p.id for p in self.points}) != len(self.points):
            raise ValueError("Duplicate vector point identity")
        return self

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json")).encode()

    @property
    def identity_sha256(self) -> str:
        return hashlib.sha256(
            "\n".join(f"{p.id}:{p.ordinal}:{p.text_sha256}" for p in self.points).encode()
        ).hexdigest()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()
