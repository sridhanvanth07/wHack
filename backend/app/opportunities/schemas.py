from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, conint, field_validator, model_validator

from backend.app.discovery.schemas import Opportunity


ApplicationStatus = Literal["SAVED", "PREPARING", "APPLIED", "INTERVIEW", "OFFER", "REJECTED", "WITHDRAWN", "EXPIRED"]


class SaveOpportunityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    opportunity_id: str = Field(min_length=1, max_length=256)


class StatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ApplicationStatus


class NoteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Note cannot be blank")
        return value


class NoteUpdateRequest(NoteCreateRequest):
    pass


class ChatSession(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(default="default", min_length=1, max_length=64)


class SaveChatArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinals: list[conint(ge=1, le=20)] = Field(default_factory=list, max_length=20)
    company: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def require_reference(self):
        if bool(self.ordinals) == bool(self.company):
            raise ValueError("Specify result numbers or one company reference")
        if len(set(self.ordinals)) != len(self.ordinals):
            raise ValueError("Repeated result numbers are not allowed")
        return self


class SavedReferenceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinal: conint(ge=1, le=1000) | None = None
    company: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def require_reference(self):
        if bool(self.ordinal) == bool(self.company):
            raise ValueError("Specify one saved listing number or company")
        return self


class ApplicationPreviewArguments(SavedReferenceArguments):
    pass


class ApplicationPreparationLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ordinal: conint(ge=1, le=1000) | None = None
    company: str | None = Field(default=None, max_length=120)


class RemoveChatArguments(SavedReferenceArguments):
    pass


class StatusChatArguments(SavedReferenceArguments):
    status: ApplicationStatus
    from_search: bool = False


class ListChatArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ApplicationStatus | None = None
    sort_by_deadline: bool = False


class HistoryChatArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str | None = Field(default=None, max_length=120)


class NoteChatArguments(SavedReferenceArguments):
    note: str = Field(min_length=1, max_length=2000)
    from_search: bool = False

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Note cannot be blank")
        return value


class OpportunityNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    note: str
    created_at: datetime
    updated_at: datetime


class SavedOpportunityRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    opportunity: Opportunity
    status: ApplicationStatus
    saved_at: datetime
    updated_at: datetime
    applied_at: datetime | None = None
    notes: list[OpportunityNote] = Field(default_factory=list)


class SavedOpportunityList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunities: list[SavedOpportunityRecord]


class SavedOpportunityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    opportunity: SavedOpportunityRecord
    created: bool


class StatusHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    opportunity_id: str
    previous_status: ApplicationStatus | None = None
    new_status: ApplicationStatus
    changed_at: datetime
    title: str | None = None
    company_name: str | None = None


class StatusHistoryList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    history: list[StatusHistoryEntry]


class OperationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    removed: bool | None = None
    deleted: bool | None = None
