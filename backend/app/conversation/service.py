from __future__ import annotations

from .executor import execute_tool
from .intents import interpret
from .policy import ToolPolicyError
from .schemas import ChatRequest, ChatResponse, ToolCall


def handle_message(request: ChatRequest) -> ChatResponse:
    from backend.app import main
    workflows = main.workflow_service()
    lowered = request.message.casefold().strip()
    active = workflows.active(request.session_id)
    for verb, words in (("pause", ("pause this workflow", "pause the workflow", "pause my internship search")), ("resume", ("resume this workflow", "resume the workflow", "continue my workflow", "resume my internship search", "continue my internship search")), ("cancel", ("cancel this workflow", "cancel the workflow"))):
        if any(word in lowered for word in words):
            if not active:
                return ChatResponse(assistant_message="There isn’t an active workflow in this chat yet.", tool_status="not_run")
            try:
                updated = workflows.transition(active["id"], verb)
                return ChatResponse(assistant_message=f"Workflow {updated['status'].lower().replace('_',' ')}. {updated.get('pending_action') or 'I saved the latest checkpoint.'}", tool_status="completed")
            except ValueError as exc:
                return ChatResponse(assistant_message=str(exc), tool_status="error")
    if any(word in lowered for word in ("complete this workflow", "mark this workflow complete", "i'm done with this workflow")):
        if not active:
            return ChatResponse(assistant_message="There isn’t an active workflow in this chat yet.", tool_status="not_run")
        try:
            updated = workflows.transition(active["id"], "complete")
            return ChatResponse(assistant_message=f"Workflow marked {updated['status'].lower()}.", tool_status="completed")
        except ValueError as exc:
            return ChatResponse(assistant_message=str(exc), tool_status="error")
    previous_search = None
    try:
        from backend.app.conversation.tools import _opportunity_service
        previous_search = _opportunity_service().latest_search_request(request.session_id)
    except Exception:
        previous_search = None
    intent = interpret(request.message, previous_search=previous_search)
    if intent.tool_name is None:
        return ChatResponse(assistant_message=intent.reply, tool_status="not_run")

    call = ToolCall(name=intent.tool_name, arguments=intent.arguments)
    read_only_tools = {"get_workflow_status", "list_saved_opportunities", "get_application_history", "get_application_preparation", "get_recent_search", "list_resumes", "get_resume_extraction", "search_passport", "compare_resume"}
    if active and active["status"] == "PAUSED" and call.name not in read_only_tools:
        return ChatResponse(assistant_message="This workflow is paused. Say ‘resume this workflow’ before continuing.", tool_status="not_run")
    if not active and call.name == "search_internships":
        try:
            active = workflows.create(request.message, request.session_id)
        except ValueError:
            active = None
    try:
        result = execute_tool(call, request.session_id)
    except ToolPolicyError as exc:
        if active:
            workflows.record_tool(request.session_id, call.name, None, str(exc))
        return ChatResponse(
            assistant_message=str(exc),
            tool_status="error",
            tool=None,
        )
    except Exception:
        # Avoid returning internal errors, stack traces, or database records to the UI.
        if active:
            workflows.record_tool(request.session_id, call.name, None, "Action failed; inspect saved state before retrying.")
        return ChatResponse(
            assistant_message="I couldn’t complete that workflow action. Please check the dashboard and try again.",
            tool_status="error",
            tool=None,
        )

    if not active and call.name in {"save_opportunity", "preview_application", "get_application_preparation"}:
        try:
            active = workflows.create(request.message, request.session_id)
        except ValueError:
            active = None
    if active and active["status"] != "PAUSED":
        workflows.record_tool(request.session_id, call.name, result)
    return ChatResponse(
        assistant_message=result.summary,
        tool_status="completed",
        tool=result,
    )
