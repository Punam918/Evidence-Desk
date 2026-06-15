from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

Role = Literal["viewer", "editor", "admin", "owner"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class WorkspaceInput(Input):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Name cannot be blank")
        return value.strip()


class MemberInput(Input):
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    role: Role


class RoleInput(Input):
    role: Role
    expected_version: int = Field(ge=1)


class SettingsInput(Input):
    privacy_mode: Literal["hosted_allowed", "local_only"]
    daily_budget_usd: str = Field(pattern=r"^\d+\.\d{2}$", max_length=15)
    monthly_budget_usd: str = Field(pattern=r"^\d+\.\d{2}$", max_length=15)
    retrieval_profile: Literal["dense", "hybrid", "hybrid_rerank", "hybrid_rewrite"]
    top_k: int = Field(ge=1, le=8)
    summaries_enabled: bool
    expected_version: int = Field(ge=1)


class Workspace(BaseModel):
    id: UUID
    name: str
    role: Role
    created_at: datetime
    version: int


class Member(BaseModel):
    user_id: UUID
    email: str
    role: Role
    version: int


class MemberRecord(Member):
    created_at: datetime


class PolicyRecord(BaseModel):
    id: UUID
    privacy_mode: Literal["hosted_allowed", "local_only"]
    daily_budget_micro_usd: int
    monthly_budget_micro_usd: int
    retrieval_profile: Literal["dense", "hybrid", "hybrid_rerank", "hybrid_rewrite"]
    top_k: int
    summaries_enabled: bool
    version: int


class Policy(BaseModel):
    privacy_mode: Literal["hosted_allowed", "local_only"]
    daily_budget_usd: str
    monthly_budget_usd: str
    retrieval_profile: Literal["dense", "hybrid", "hybrid_rerank", "hybrid_rewrite"]
    top_k: int
    summaries_enabled: bool
    version: int


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None
