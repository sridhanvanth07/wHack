from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.app import main
from backend.app.conversation.service import handle_message
from backend.app.conversation.schemas import ChatRequest
from backend.app.discovery.schemas import DiscoveryResponse, Opportunity, SourceStatus
from backend.app.opportunities.schemas import NoteCreateRequest, SaveOpportunityRequest, StatusUpdateRequest
from backend.app.opportunities.service import OpportunityNotFound, OpportunityService, init_schema


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "opportunities.db"

    def connect():
        import sqlite3
        connection = sqlite3.connect(path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    with connect() as connection:
        init_schema(connection)
    return OpportunityService(connect)


def opportunity(**overrides):
    data = {
        "id": "remotive-101",
        "title": "Software Engineering Intern",
        "company_name": "Example Labs",
        "location": "Remote, India",
        "work_mode": "remote",
        "compensation_text": "INR 20,000 / month",
        "compensation_min": 20000,
        "compensation_currency": "INR",
        "skills": ["Python"],
        "application_deadline": date(2027, 1, 30),
        "listing_url": "https://remotive.com/remote-jobs/software/intern-101?utm_source=test",
        "source_name": "Remotive",
        "source_listing_id": "101",
        "retrieved_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return Opportunity(**data)


def seed_context(service, session="test-session", items=None):
    items = items or [opportunity()]
    service.remember_search(session, items)
    return items


def test_latest_search_request_is_persisted_per_session(service):
    service.remember_search("chat-a", [opportunity()], {"keywords": "Python", "result_count": 10})
    assert service.latest_search_request("chat-a") == {"keywords": "Python", "result_count": 10}
    assert service.latest_search_request("chat-b") is None


def test_save_duplicate_unsave_and_reinitialize_preserve_snapshot(service):
    item = seed_context(service)[0]
    record, created = service.save_from_context("test-session", item.id)
    assert created is True
    assert record["status"] == "SAVED"
    assert record["opportunity"]["application_deadline"] == "2027-01-30"
    assert record["opportunity"]["source_attributions"][0]["listing_url"].startswith("https://remotive.com/")
    assert record["saved_at"]

    duplicate, created_again = service.save_from_context("test-session", item.id)
    assert created_again is False
    assert duplicate["id"] == record["id"]
    assert len(service.list_saved()) == 1

    # A new service instance against the same DB simulates application reinitialization.
    assert OpportunityService(service._connection_factory).get_saved(record["id"])["opportunity"]["title"] == item.title
    assert service.remove(record["id"])
    assert not service.remove(record["id"])
    assert service.list_saved() == []
    assert service.history(record["id"])
    re_saved, reactivated = service.save_from_context("test-session", item.id)
    assert reactivated is True
    assert re_saved["id"] == record["id"]
    assert len(service.history(record["id"])) == 2


def test_save_requires_backend_owned_recent_search_reference(service):
    with pytest.raises(OpportunityNotFound):
        service.save_from_context("test-session", "attacker-controlled-id")
    seed_context(service, "another-session")
    with pytest.raises(OpportunityNotFound):
        service.save_from_context("test-session", "remotive-101")
    with pytest.raises(ValueError, match="Invalid saved opportunity ID"):
        service.get_saved("../not-a-uuid")


def test_status_transitions_are_persisted_and_history_is_idempotent(service):
    item = seed_context(service)[0]
    record, _ = service.save_from_context("test-session", item.id)
    applied = service.update_status(record["id"], "APPLIED")
    assert applied["status"] == "APPLIED"
    assert applied["applied_at"]
    service.update_status(record["id"], "APPLIED")
    service.update_status(record["id"], "INTERVIEW")
    rows = service.history(record["id"])
    assert [row["new_status"] for row in rows] == ["INTERVIEW", "APPLIED", "SAVED"]
    assert rows[1]["previous_status"] == "SAVED"
    assert service.get_saved(record["id"])["status"] == "INTERVIEW"
    assert len(service.list_saved(status="INTERVIEW")) == 1
    assert len(service.list_saved(status="SAVED")) == 0


def test_deadline_sort_puts_known_source_deadlines_first_and_unknown_last(service):
    first = opportunity()
    second = opportunity(id="remotive-102", title="Data Intern", application_deadline=None,
                         listing_url="https://remotive.com/remote-jobs/data/intern-102", source_listing_id="102")
    third = opportunity(id="remotive-103", title="AI Intern", application_deadline=date(2026, 12, 20),
                        listing_url="https://remotive.com/remote-jobs/ai/intern-103", source_listing_id="103")
    seed_context(service, items=[first, second, third])
    for item in [first, second, third]:
        service.save_from_context("test-session", item.id)
    assert [row["opportunity"]["title"] for row in service.list_saved(sort_by_deadline=True)] == ["AI Intern", first.title, "Data Intern"]


def test_notes_are_private_crud_and_validated(service):
    item = seed_context(service)[0]
    record, _ = service.save_from_context("test-session", item.id)
    note = service.add_note(record["id"], " Ask the recruiter about timing. ")
    assert note["note"] == "Ask the recruiter about timing."
    updated = service.update_note(record["id"], note["id"], "Confirm the start date.")
    assert updated["note"] == "Confirm the start date."
    assert service.get_saved(record["id"])["notes"][0]["note"] == "Confirm the start date."
    assert service.delete_note(record["id"], note["id"])
    assert not service.delete_note(record["id"], note["id"])
    with pytest.raises(ValidationError):
        NoteCreateRequest(note="   ")


def test_session_scoped_chat_context_and_save_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB", tmp_path / "chat-opportunities.db")
    main.init()
    items = [opportunity(), opportunity(id="remotive-102", title="Data Intern", company_name="Other Labs",
             source_listing_id="102", listing_url="https://remotive.com/remote-jobs/data/intern-102")]
    response = DiscoveryResponse(
        opportunities=items,
        sources=[SourceStatus(source_name="Remotive", status="success", result_count=2, message="OK")],
        query_summary="Python", assistant_summary="Found two internships.",
    )
    import backend.app.conversation.tools as tools
    monkeypatch.setattr(tools, "discover", lambda request: response)

    session = "session-a"
    found = handle_message(ChatRequest(message="Find Python internships", session_id=session))
    assert found.tool_status == "completed"
    assert found.tool.tool_name == "search_internships"
    saved = handle_message(ChatRequest(message="Save the first one", session_id=session))
    assert "Saved 1" in saved.assistant_message
    listed = handle_message(ChatRequest(message="What have I saved?", session_id=session))
    assert listed.tool.data["opportunities"][0]["opportunity"]["title"] == items[0].title

    other = handle_message(ChatRequest(message="Save the first one", session_id="session-b"))
    assert other.tool_status == "error"
    applied = handle_message(ChatRequest(message="I applied to this one today", session_id=session))
    assert applied.tool.data["status"] == "APPLIED"
    assert applied.tool.data["applied_at"]


def test_chat_clarifies_ambiguous_company_and_handles_top_three(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB", tmp_path / "chat-ambiguity.db")
    main.init()
    items = [opportunity(id=f"same-{i}", title=f"Engineering Intern {i}", source_listing_id=str(i),
                         listing_url=f"https://remotive.com/remote-jobs/software/intern-{i}") for i in range(1, 4)]
    response = DiscoveryResponse(opportunities=items, sources=[], query_summary="Engineering", assistant_summary="Found three.")
    import backend.app.conversation.tools as tools
    monkeypatch.setattr(tools, "discover", lambda request: response)
    session = "ambiguity-session"
    handle_message(ChatRequest(message="Find engineering internships", session_id=session))
    ambiguous = handle_message(ChatRequest(message="Save Example Labs internship", session_id=session))
    assert ambiguous.tool_status == "error"
    assert "multiple matches" in ambiguous.assistant_message
    top_three = handle_message(ChatRequest(message="Save the top three", session_id=session))
    assert top_three.tool.data["matched_count"] == 3


def test_chat_parser_routes_management_without_confusing_it_for_discovery():
    from backend.app.conversation.intents import interpret

    assert interpret("Show my saved internships").tool_name == "list_saved_opportunities"
    assert interpret("Sort my saved jobs by deadline").arguments["sort_by_deadline"] is True
    assert interpret("What applications are currently in interview?").arguments["status"] == "INTERVIEW"
    assert interpret("Remove the second saved internship").arguments["ordinal"] == 2
    mark = interpret("Mark the internship at Company X as applied")
    assert mark.tool_name == "update_application_status"
    assert mark.arguments["company"] == "Company X"
    note = interpret("Add a note to this internship: follow up next week")
    assert note.tool_name == "add_opportunity_note"
    assert note.arguments["note"] == "follow up next week"


def test_opportunity_api_save_status_notes_and_stale_reference(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB", tmp_path / "opportunity-api.db")
    main.init()
    item = opportunity()
    from backend.app.opportunities.service import OpportunityService
    OpportunityService(main.db).remember_search("api-session", [item])
    with pytest.raises(HTTPException) as refused:
        main.save_opportunity(SaveOpportunityRequest(session_id="api-session", opportunity_id="spoofed"))
    assert refused.value.status_code == 404
    saved = main.save_opportunity(SaveOpportunityRequest(session_id="api-session", opportunity_id=item.id))
    record = saved["opportunity"]
    changed = main.update_saved_opportunity_status(record["id"], StatusUpdateRequest(status="APPLIED"))
    assert changed["applied_at"]
    note = main.create_opportunity_note(record["id"], NoteCreateRequest(note="Follow up"))
    assert note["note"] == "Follow up"
    assert main.get_saved_opportunity(record["id"])["notes"][0]["note"] == "Follow up"
    assert main.get_opportunity_history(record["id"])["history"]
    with pytest.raises(ValidationError):
        StatusUpdateRequest(status="UNKNOWN")
