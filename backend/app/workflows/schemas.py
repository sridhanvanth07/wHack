from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=5, max_length=1000)
    session_id: str = Field(default="default", min_length=1, max_length=64)

    @field_validator("session_id")
    @classmethod
    def valid_session_id(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Session ID is invalid.")
        return value


class WorkflowDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(min_length=1, max_length=500)

    @field_validator("decision")
    @classmethod
    def decision_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Decision cannot be blank.")
        return value
