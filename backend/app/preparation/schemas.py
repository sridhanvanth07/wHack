from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class PreparationStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunity_id: str = Field(min_length=1, max_length=64)


class OpportunityPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunity_id: str
    title: str
    company_name: str | None = None
    destination_url: str
    origin: str
    origin_approved: bool


class OriginApprovalList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origins: list[OriginApproval]


class OriginApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(min_length=9, max_length=300)


class FieldReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact_id: str | None = Field(default=None, max_length=64)
    decision: Literal["approve", "reject", "blank", "map"]


class PreparationApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class FormField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str
    fingerprint: str
    field_type: str
    label: str
    name: str
    element_id: str
    placeholder: str
    required: bool
    options: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    visible: bool
    accessible_name: str
    description: str
    mapping_status: Literal["PROPOSED", "AMBIGUOUS", "UNSUPPORTED", "MISSING_FACT", "PERMISSION_BLOCKED", "APPROVED", "REJECTED", "BLANK"]
    semantic_type: str = "unknown"
    fact_id: str | None = None
    fact_label: str | None = None
    value_preview: str | None = None
    value_transform: Literal["first_name", "last_name"] | None = None
    mapping_confidence: float = Field(ge=0, le=1)
    explanation: str
    user_approved: bool = False


class PreparationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    workflow_id: str
    opportunity_id: str
    origin: str
    prepared_at: str
    detected_field_count: int
    approved_mapping_count: int
    successfully_filled_fields: list[str]
    skipped_fields: list[str]
    permission_blocked_fields: list[str]
    ambiguous_fields: list[str]
    failed_fields: dict[str, str]
    recovery_state: str
    user_approval_reference: str | None = None


class PreparationState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    opportunity_id: str
    company_name: str | None
    title: str
    destination_url: str
    origin: str
    status: str
    fields: list[FormField]
    receipt: PreparationReceipt | None = None
    created_at: str
    updated_at: str


class PreparationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preparation: PreparationState


class PreparationList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preparations: list[PreparationState]


class OriginApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str
    approved_at: str
    approved_by: Literal["user"]
