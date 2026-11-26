from __future__ import annotations

from pydantic import BaseModel, ConfigDict, ValidationError

from backend.app.discovery.schemas import SearchRequest
from backend.app.opportunities.schemas import (
    HistoryChatArguments, ListChatArguments, NoteChatArguments, RemoveChatArguments,
    ApplicationPreparationLookupArguments, ApplicationPreviewArguments, SaveChatArguments, StatusChatArguments,
)
from backend.app.resumes.schemas import PassportQueryArguments, ResumeLookupArguments
from .schemas import ToolCall


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreparePortalArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    portal: str


class ToolPolicyError(ValueError):
    """Raised when a proposed tool call is outside Phase 1 policy."""


ARGUMENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "get_workflow_status": EmptyArguments,
    "start_demo": EmptyArguments,
    "prepare_local_portal": PreparePortalArguments,
    "search_internships": SearchRequest,
    "get_recent_search": EmptyArguments,
    "save_opportunity": SaveChatArguments,
    "list_saved_opportunities": ListChatArguments,
    "remove_saved_opportunity": RemoveChatArguments,
    "update_application_status": StatusChatArguments,
    "get_application_history": HistoryChatArguments,
    "add_opportunity_note": NoteChatArguments,
    "list_resumes": EmptyArguments,
    "get_resume_extraction": ResumeLookupArguments,
    "search_passport": PassportQueryArguments,
    "compare_resume": ResumeLookupArguments,
    "preview_application": ApplicationPreviewArguments,
    "get_application_preparation": ApplicationPreparationLookupArguments,
}

ALLOWED_PORTALS = {"northstar", "summit_talent"}


def validate_tool_call(call: ToolCall) -> BaseModel:
    schema = ARGUMENT_SCHEMAS.get(call.name)
    if schema is None:
        raise ToolPolicyError("That workflow action is not available in CarryFlow chat.")
    try:
        parsed = schema.model_validate(call.arguments)
    except ValidationError as exc:
        raise ToolPolicyError("The requested action has invalid arguments. Please clarify and try again.") from exc

    if call.name == "prepare_local_portal" and getattr(parsed, "portal", None) not in ALLOWED_PORTALS:
        raise ToolPolicyError("Only the Northstar and Summit Talent local demo portals can be prepared.")
    return parsed
