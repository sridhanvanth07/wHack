from __future__ import annotations

from .policy import ToolPolicyError, validate_tool_call
from .schemas import ToolCall, ToolResult
from .tools import TOOL_REGISTRY


def execute_tool(call: ToolCall, session_id: str = "default") -> ToolResult:
    """Validate, authorize, and execute exactly one registered workflow tool."""
    parsed_args = validate_tool_call(call)
    definition = TOOL_REGISTRY.get(call.name)
    if definition is None:
        # Keep the registry and policy allowlist fail-closed if they ever diverge.
        raise ToolPolicyError("That workflow action is not available in CarryFlow chat.")
    if call.name == "search_internships":
        result = definition.handler(parsed_args)
        from backend.app.discovery.schemas import Opportunity
        from backend.app.conversation.tools import _opportunity_service
        items = [Opportunity.model_validate(item) for item in result.data.get("opportunities", [])]
        _opportunity_service().remember_search(session_id, items, parsed_args.model_dump(mode="json"))
        return result
    if call.name in {"save_opportunity", "list_saved_opportunities", "remove_saved_opportunity", "update_application_status", "get_application_history", "add_opportunity_note"}:
        return definition.handler(parsed_args, session_id)
    if call.name == "get_recent_search":
        return definition.handler(parsed_args, session_id)
    return definition.handler(parsed_args)
