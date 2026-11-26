from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel

from backend.app.discovery import SearchRequest, discover
from backend.app.opportunities.schemas import (
    HistoryChatArguments, ListChatArguments, NoteChatArguments, RemoveChatArguments,
    ApplicationPreparationLookupArguments, ApplicationPreviewArguments, SaveChatArguments, StatusChatArguments,
)
from backend.app.opportunities.service import AmbiguousReference, OpportunityNotFound, OpportunityService
from backend.app.resumes.schemas import PassportQueryArguments, ResumeLookupArguments
from backend.app.resumes.service import ResumeService
from .policy import EmptyArguments, PreparePortalArguments, ToolPolicyError
from .schemas import ToolResult


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[..., ToolResult]


def _opportunity_service() -> OpportunityService:
    from backend.app import main

    return OpportunityService(main.db)


def _resume_service() -> ResumeService:
    from backend.app import main

    return ResumeService(main.db)


def _status(_: BaseModel) -> ToolResult:
    from backend.app import main

    state = main.state()
    meta = state.get("meta", {})
    workflow_status = meta.get("status", "PENDING")
    facts = state.get("facts", [])
    conflicts = state.get("conflicts", [])
    receipts = state.get("receipts", [])
    actions = state.get("actions", [])
    summary = (
        f"The workflow is {workflow_status.replace('_', ' ').lower()}. "
        f"There are {len(facts)} saved profile facts, {len(receipts)} handoff receipts, "
        f"and {len(conflicts)} unresolved conflicts."
    )
    return ToolResult(
        tool_name="get_workflow_status",
        summary=summary,
        data={
            "status": workflow_status,
            "fact_count": len(facts),
            "receipt_count": len(receipts),
            "action_count": len(actions),
            "unresolved_conflict_count": len(conflicts),
        },
    )


def _start(_: BaseModel) -> ToolResult:
    from backend.app import main

    state = main.start(main.StartBody())
    return ToolResult(
        tool_name="start_demo",
        summary=(f"The local demo workflow is ready with {len(state.get('facts', []))} synthetic Passport facts. "
                 "You can ask me to prepare Northstar or Summit Talent."),
        data={"status": state.get("meta", {}).get("status"), "fact_count": len(state.get("facts", []))},
    )


def _prepare(args: BaseModel) -> ToolResult:
    from backend.app import main

    state = main.state()
    status = state.get("meta", {}).get("status", "PENDING")
    if status == "PENDING":
        raise ToolPolicyError("Start the demo workflow before preparing a portal.")

    portal_alias = getattr(args, "portal")
    portal_key = "a" if portal_alias == "northstar" else "b"
    try:
        state = main.prepare(portal_key)
    except Exception as exc:
        # Existing HTTP errors are converted into user-facing tool errors by the service.
        raise ToolPolicyError("CarryFlow could not prepare that portal. Check the workflow and try again.") from exc

    portal = main.PORTALS[portal_key]
    origin = portal["url"]
    receipts = [receipt for receipt in state.get("receipts", []) if receipt.get("origin") == origin]
    waiting = sum(receipt.get("status") == "WAITING_FOR_USER" for receipt in receipts)
    proposed = sum(receipt.get("status") == "PROPOSED" for receipt in receipts)
    return ToolResult(
        tool_name="prepare_local_portal",
        summary=(f"{portal['name']} is prepared. {proposed} fields are eligible for review; "
                 f"{waiting} field(s) need your review. No application was submitted."),
        data={"portal": portal_alias, "receipt_count": len(receipts), "proposed_count": proposed, "waiting_count": waiting},
    )


def _search_internships(args: BaseModel) -> ToolResult:
    response = discover(args)  # SearchRequest is validated by the tool policy before this point.
    payload = response.model_dump(mode="json")
    return ToolResult(
        tool_name="search_internships",
        summary=response.assistant_summary,
        data={
            "opportunities": payload["opportunities"],
            "sources": payload["sources"],
            "retrieved_at": payload["retrieved_at"],
            "query_summary": payload["query_summary"],
        },
    )


def _get_recent_search(_: BaseModel, session_id: str) -> ToolResult:
    items = _opportunity_service().search_context(session_id)
    if not items:
        return ToolResult(tool_name="get_recent_search", summary="There are no recent internship results in this chat. Run a search first.", data={"opportunities": []})
    opportunities = [item["opportunity"] for item in items]
    return ToolResult(tool_name="get_recent_search", summary=f"Here are the {len(opportunities)} opportunities from this chat’s latest search.", data={"opportunities": opportunities})


def _reference_error(exc: Exception) -> ToolPolicyError:
    if isinstance(exc, AmbiguousReference):
        names = ", ".join(f"{item.get('company_name') or 'Unknown company'} — {item.get('title')}" for item in exc.choices[:5])
        return ToolPolicyError(f"I found multiple matches. Which one did you mean? {names}")
    if isinstance(exc, OpportunityNotFound):
        return ToolPolicyError(str(exc))
    return ToolPolicyError("I couldn’t identify that opportunity. Please use its number or a more specific company name.")


def _save_opportunity(args: BaseModel, session_id: str) -> ToolResult:
    svc = _opportunity_service()
    try:
        targets = []
        parsed = args
        if parsed.ordinals:
            context = svc.search_context(session_id)
            for ordinal in parsed.ordinals:
                if ordinal <= len(context):
                    targets.append(context[ordinal - 1])
        else:
            targets = svc.resolve_context(session_id, company=parsed.company)
    except Exception as exc:
        raise _reference_error(exc) from exc
    if not targets:
        raise ToolPolicyError("I couldn’t find that listing in this chat’s recent search results. Search again, then save a result.")
    saved, skipped = [], 0
    for target in targets:
        try:
            item = target["opportunity"]
            record, created = svc.save_from_context(session_id, target.get("opportunity_id") or item["id"])
            (saved if created else []).append(record)
            if not created:
                skipped += 1
        except Exception as exc:
            raise _reference_error(exc) from exc
    total = len(saved) + skipped
    summary = f"Saved {len(saved)} opportunity{'ies' if len(saved) != 1 else ''}."
    if skipped:
        summary += f" {skipped} {'was' if skipped == 1 else 'were'} already saved."
    return ToolResult(tool_name="save_opportunity", summary=summary, data={"saved": saved, "already_saved": skipped, "matched_count": total})


def _list_saved_opportunities(args: BaseModel, _: str) -> ToolResult:
    parsed = args
    records = _opportunity_service().list_saved(parsed.status, parsed.sort_by_deadline)
    if not records:
        return ToolResult(tool_name="list_saved_opportunities", summary="You don’t have any saved opportunities matching that filter yet.", data={"opportunities": [], "status_filter": parsed.status, "sort_by_deadline": parsed.sort_by_deadline})
    order = " by known deadlines first" if parsed.sort_by_deadline else ""
    return ToolResult(tool_name="list_saved_opportunities", summary=f"Here are {len(records)} saved opportunit{'y' if len(records)==1 else 'ies'}{order}.", data={"opportunities": records, "status_filter": parsed.status, "sort_by_deadline": parsed.sort_by_deadline})


def _remove_saved_opportunity(args: BaseModel, _: str) -> ToolResult:
    parsed = args
    svc = _opportunity_service()
    try:
        matches = svc.resolve_saved(ordinal=parsed.ordinal, company=parsed.company)
        if not matches:
            raise OpportunityNotFound("I couldn’t find a saved opportunity with that number or company.")
        record = matches[0]
        svc.remove(record["id"])
        title = record["opportunity"].get("title", "That listing")
        return ToolResult(tool_name="remove_saved_opportunity", summary=f"Removed {title} from your saved opportunities.", data={"removed_id": record["id"]})
    except Exception as exc:
        raise _reference_error(exc) from exc


def _update_application_status(args: BaseModel, session_id: str) -> ToolResult:
    parsed = args
    svc = _opportunity_service()
    try:
        matches = svc.resolve_context(session_id, ordinal=parsed.ordinal, company=parsed.company) if parsed.from_search else []
        if not matches and not parsed.from_search and parsed.company:
            matches = svc.resolve_saved(company=parsed.company)
            if not matches:
                matches = svc.resolve_context(session_id, company=parsed.company)
        elif not parsed.from_search:
            matches = svc.resolve_saved(ordinal=parsed.ordinal, company=parsed.company)
        if not matches:
            raise OpportunityNotFound("I couldn’t find that opportunity in the recent search results or saved list.")
        record = matches[0]
        if record.get("opportunity_id"):
            record, _ = svc.save_from_context(session_id, record["opportunity_id"])
        updated = svc.update_status(record["id"], parsed.status)
        title = updated["opportunity"].get("title", "Opportunity")
        return ToolResult(tool_name="update_application_status", summary=f"{title} is now marked {parsed.status.lower()}. This records the status you reported; it doesn’t submit anything.", data=updated)
    except Exception as exc:
        raise _reference_error(exc) from exc


def _get_application_history(args: BaseModel, _: str) -> ToolResult:
    history = _opportunity_service().history(company=args.company)
    return ToolResult(tool_name="get_application_history", summary=f"There are {len(history)} status history record{'s' if len(history) != 1 else ''}.", data={"history": history})


def _add_opportunity_note(args: BaseModel, session_id: str) -> ToolResult:
    parsed = args
    svc = _opportunity_service()
    try:
        matches = svc.resolve_context(session_id, ordinal=parsed.ordinal, company=parsed.company) if parsed.from_search else []
        if not parsed.from_search and parsed.company:
            matches = svc.resolve_saved(company=parsed.company)
            if not matches:
                matches = svc.resolve_context(session_id, company=parsed.company)
        elif not parsed.from_search:
            matches = svc.resolve_saved(ordinal=parsed.ordinal, company=parsed.company)
        if not matches:
            raise OpportunityNotFound("I couldn’t find that opportunity in the recent search results or saved list.")
        record = matches[0]
        if record.get("opportunity_id"):
            record, _ = svc.save_from_context(session_id, record["opportunity_id"])
        note = svc.add_note(record["id"], parsed.note)
        return ToolResult(tool_name="add_opportunity_note", summary="Added a private note to that saved opportunity.", data={"opportunity_id": record["id"], "note": note})
    except Exception as exc:
        raise _reference_error(exc) from exc


def _list_resumes(_: BaseModel) -> ToolResult:
    resumes = _resume_service().list_resumes()
    summary = "No resume is uploaded yet. Open the Resume tab to upload a PDF or DOCX." if not resumes else f"You have {len(resumes)} resume record(s). Extracted items remain unverified until you accept them."
    return ToolResult(tool_name="list_resumes", summary=summary, data={"resumes": resumes})


def _get_resume_extraction(args: BaseModel) -> ToolResult:
    parsed: ResumeLookupArguments = args
    resumes = _resume_service().list_resumes()
    if parsed.filename:
        matches = [resume for resume in resumes if parsed.filename.casefold() in resume["filename"].casefold()]
    else:
        matches = resumes
    if not matches:
        return ToolResult(tool_name="get_resume_extraction", summary="I couldn’t find a matching resume. Use the Resume tab to upload one.", data={"items": [], "resumes": []})
    if len(matches) > 1:
        return ToolResult(tool_name="get_resume_extraction", summary="You have multiple resumes. Which one should I review? " + ", ".join(x["filename"] for x in matches[:8]), data={"items": [], "resumes": matches})
    detail = _resume_service().get_resume(matches[0]["id"])
    items = detail["items"]
    if parsed.category:
        items = [item for item in items if item["category"] == parsed.category]
    return ToolResult(tool_name="get_resume_extraction", summary=f"Showing {len(items)} {parsed.category or 'extracted'} item(s) from {detail['resume']['filename']}. Items marked EXTRACTED still need your review.", data={"resume": detail["resume"], "items": items})


def _search_passport(args: BaseModel) -> ToolResult:
    from backend.app import main

    parsed: PassportQueryArguments = args
    query = parsed.query.casefold()
    passport = main.get_passport()
    matches = []
    for group in passport["passport"]:
        active = group["active"]
        text = " ".join(str(active.get(key) or "") for key in ("id", "key", "semantic_type", "source_label", "value", "source_section")).casefold()
        if query in text:
            matches.append({"active": active, "has_conflict": group["has_conflict"], "version_count": group["version_count"]})
    summary = f"I found {len(matches)} matching Passport fact(s)." if matches else f"I couldn’t find a Passport fact matching “{parsed.query}”."
    return ToolResult(tool_name="search_passport", summary=summary, data={"facts": matches})


def _compare_resume(args: BaseModel) -> ToolResult:
    parsed: ResumeLookupArguments = args
    resumes = _resume_service().list_resumes()
    matches = [resume for resume in resumes if parsed.filename and parsed.filename.casefold() in resume["filename"].casefold()] if parsed.filename else resumes
    if not matches:
        return ToolResult(tool_name="compare_resume", summary="I couldn’t find a resume to compare. Use the Resume tab to upload one.", data={"comparisons": []})
    if len(matches) > 1:
        return ToolResult(tool_name="compare_resume", summary="You have multiple resumes. Which one should I compare? " + ", ".join(x["filename"] for x in matches[:8]), data={"comparisons": [], "resumes": matches})
    result = _resume_service().compare_with_passport(matches[0]["id"])
    counts = {key: sum(item["comparison"] == key for item in result["comparisons"]) for key in ("matches", "different", "not_in_passport")}
    return ToolResult(tool_name="compare_resume", summary=f"Compared {len(result['comparisons'])} extracted item(s): {counts['matches']} match, {counts['different']} differ, and {counts['not_in_passport']} are not in the Passport. This comparison does not change any facts.", data=result)


def _preview_application(args: BaseModel, _: str = "default") -> ToolResult:
    from backend.app import main
    service = _opportunity_service()
    records = service.list_saved()
    ordinal = getattr(args, "ordinal", None)
    company = getattr(args, "company", None)
    if ordinal:
        if ordinal > len(records):
            raise OpportunityNotFound("That saved opportunity number is not in the list.")
        record = records[ordinal - 1]
    else:
        matches = [row for row in records if company.casefold() in (row["opportunity"].get("company_name") or "").casefold()]
        if len(matches) != 1:
            raise ToolPolicyError("That company name is ambiguous or is not saved. Open Saved opportunities and choose one.")
        record = matches[0]
    preview = main.preview_preparation(record["id"])
    return ToolResult(tool_name="preview_application", summary=(f"{preview['title']} at {preview.get('company_name') or 'the selected company'} uses {preview['origin']}. "
        f"The origin {'is already approved' if preview['origin_approved'] else 'needs your review and separate approval'}. Open Saved opportunities to review and start preparation; chat will not open the site or fill fields."), data=preview)


def _get_application_preparation(args: BaseModel, _: str = "default") -> ToolResult:
    from backend.app import main
    records = main.preparation_service().list()
    ordinal = getattr(args, "ordinal", None)
    company = getattr(args, "company", None)
    if ordinal or company:
        saved = _opportunity_service().list_saved()
        if ordinal:
            if ordinal > len(saved):
                raise OpportunityNotFound("That saved opportunity number is not in the list.")
            opportunity_id = saved[ordinal - 1]["id"]
        else:
            matches = [x for x in saved if company.casefold() in (x["opportunity"].get("company_name") or "").casefold()]
            if len(matches) != 1:
                raise ToolPolicyError("That company name is ambiguous or is not saved.")
            opportunity_id = matches[0]["id"]
        records = [x for x in records if x["opportunity_id"] == opportunity_id]
    if not records:
        return ToolResult(tool_name="get_application_preparation", summary="There is no application preparation receipt for that saved opportunity yet.", data={"preparation": None})
    prep = records[0]
    receipt = prep.get("receipt")
    data = {"id": prep["id"], "title": prep["title"], "company_name": prep.get("company_name"), "status": prep["status"], "origin": prep["origin"], "receipt": receipt}
    if not receipt:
        return ToolResult(tool_name="get_application_preparation", summary=f"{prep['title']} at {prep.get('company_name') or 'the selected company'} is {prep['status'].replace('_',' ').lower()}; there is no fill receipt yet.", data={"preparation": data})
    summary = (f"{prep['title']}: prepared {len(receipt['successfully_filled_fields'])} field(s) ({', '.join(receipt['successfully_filled_fields']) or 'none'}); "
        f"skipped {len(receipt['skipped_fields'])}, permission-blocked {len(receipt['permission_blocked_fields'])}, ambiguous {len(receipt['ambiguous_fields'])}, and failed {len(receipt['failed_fields'])}. The receipt stores no field values.")
    return ToolResult(tool_name="get_application_preparation", summary=summary, data={"preparation": data})


# The discovery addition is read-only. Do not add application or browser
# side-effecting tools here without a separate policy and approval design.
TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "get_workflow_status": ToolDefinition(
        name="get_workflow_status",
        description="Read and summarize the existing local workflow state.",
        arguments_model=EmptyArguments,
        handler=_status,
    ),
    "start_demo": ToolDefinition(
        name="start_demo",
        description="Start the existing synthetic local demo workflow.",
        arguments_model=EmptyArguments,
        handler=_start,
    ),
    "prepare_local_portal": ToolDefinition(
        name="prepare_local_portal",
        description="Prepare one allowlisted local demo portal using existing workflow checks.",
        arguments_model=PreparePortalArguments,
        handler=_prepare,
    ),
    "search_internships": ToolDefinition(
        name="search_internships",
        description="Search the supported internship sources using validated filters.",
        arguments_model=SearchRequest,
        handler=_search_internships,
    ),
    "get_recent_search": ToolDefinition("get_recent_search", "Read the latest internship results from this chat only.", EmptyArguments, _get_recent_search),
    "save_opportunity": ToolDefinition("save_opportunity", "Save one or more opportunities from the current session’s recent search results.", SaveChatArguments, _save_opportunity),
    "list_saved_opportunities": ToolDefinition("list_saved_opportunities", "List saved opportunities with optional status and deadline ordering.", ListChatArguments, _list_saved_opportunities),
    "remove_saved_opportunity": ToolDefinition("remove_saved_opportunity", "Remove a saved opportunity by list number or unambiguous company name.", RemoveChatArguments, _remove_saved_opportunity),
    "update_application_status": ToolDefinition("update_application_status", "Record a user-reported status for a saved or recent-search opportunity.", StatusChatArguments, _update_application_status),
    "get_application_history": ToolDefinition("get_application_history", "Read the persistent application status history.", HistoryChatArguments, _get_application_history),
    "add_opportunity_note": ToolDefinition("add_opportunity_note", "Add a private local note to a saved opportunity.", NoteChatArguments, _add_opportunity_note),
    "list_resumes": ToolDefinition("list_resumes", "List local resume extraction records without retrieving original files.", EmptyArguments, _list_resumes),
    "get_resume_extraction": ToolDefinition("get_resume_extraction", "Read extracted resume information for user review.", ResumeLookupArguments, _get_resume_extraction),
    "search_passport": ToolDefinition("search_passport", "Search verified and unverified Passport facts for a field or value.", PassportQueryArguments, _search_passport),
    "compare_resume": ToolDefinition("compare_resume", "Compare extracted resume information with the current Passport without changing facts.", ResumeLookupArguments, _compare_resume),
    "preview_application": ToolDefinition("preview_application", "Read the destination URL for one saved opportunity; does not open a browser or approve an origin.", ApplicationPreviewArguments, _preview_application),
    "get_application_preparation": ToolDefinition("get_application_preparation", "Read the latest field preparation receipt and counts without reading profile values or changing browser state.", ApplicationPreparationLookupArguments, _get_application_preparation),
}
