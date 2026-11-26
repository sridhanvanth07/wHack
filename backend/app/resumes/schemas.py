from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ResumeCategory = Literal[
    "personal", "professional_link", "education", "skills", "project", "experience",
    "certification", "achievement", "activity", "research", "publication", "volunteering", "coursework",
]
ReviewStatus = Literal["EXTRACTED", "EDITED", "USER_VERIFIED", "REJECTED", "CONFLICT_PENDING"]


class ResumeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: ResumeCategory
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=4000)
    original_value: str | None = Field(default=None, max_length=4000)
    details: dict[str, Any] = Field(default_factory=dict)
    target_fact_id: str = Field(min_length=1, max_length=64)
    passport_fact_id: str | None = Field(default=None, max_length=64)
    semantic_type: str = Field(min_length=1, max_length=100)
    section: str = Field(min_length=1, max_length=100)
    evidence: str = Field(min_length=1, max_length=600)
    confidence: float = Field(ge=0, le=1)
    review_status: ReviewStatus = "EXTRACTED"
    passport_version: int | None = None
    review_error: str | None = None


class ResumeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    filename: str
    file_type: Literal["PDF", "DOCX"]
    size_bytes: int = Field(gt=0, le=8_388_608)
    uploaded_at: datetime
    extraction_status: Literal["READY"]
    item_count: int = Field(ge=0)
    verified_count: int = Field(ge=0)
    warning: str | None = None


class ResumeDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume: ResumeRecord
    items: list[ResumeItem]


class ResumeList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resumes: list[ResumeRecord]


class ResumeItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=4000)
    details: dict[str, Any] | None = None

    @field_validator("value")
    @classmethod
    def value_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Resume information cannot be blank")
        return value


class ResumeReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(min_length=1, max_length=100)


class ResumeReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[ResumeItem]
    errors: dict[str, str] = Field(default_factory=dict)


class ResumeUploadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume: ResumeRecord
    items: list[ResumeItem]
    warnings: list[str] = Field(default_factory=list)


class ResumeDeleteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deleted: bool


class ResumeLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: ResumeCategory | None = None
    filename: str | None = Field(default=None, max_length=180)


class PassportQueryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=120)

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a Passport field or value to search for")
        return value
