from __future__ import annotations

from io import BytesIO

import pytest
from docx import Document

from backend.app import main
from backend.app.conversation.schemas import ChatRequest
from backend.app.conversation.service import handle_message
from backend.app.discovery.schemas import DiscoveryResponse, Opportunity, SourceStatus
from backend.app.opportunities.service import OpportunityService
from backend.app.resumes.service import ResumeService


@pytest.fixture
def integration_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", tmp_path / "phase7-integration.db")
    main.init()


def _resume_docx() -> bytes:
    document = Document()
    for line in ("Kai Example", "kai.example@example.org", "https://github.com/kaiexample", "EDUCATION", "Example University", "B.S. Computer Science", "SKILLS", "Python, FastAPI"):
        document.add_paragraph(line)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def test_journey_discovery_save_note_status_history_and_workflow_refs(integration_db, monkeypatch):
    item = Opportunity(
        id="synthetic-ai-1", title="AI Engineering Intern", company_name="Example Labs",
        listing_url="https://jobs.example.com/ai-intern", source_name="Synthetic", source_listing_id="ai-1",
    )
    response = DiscoveryResponse(
        opportunities=[item], sources=[SourceStatus(source_name="Synthetic", status="success", result_count=1, message="Fixture")],
        query_summary="AI internship", assistant_summary="Found one supported listing.",
    )
    import backend.app.conversation.tools as tools
    monkeypatch.setattr(tools, "discover", lambda _request: response)
    session = "phase7-career-chat"
    found = handle_message(ChatRequest(message="Find AI internships", session_id=session))
    assert found.tool_status == "completed"
    saved = handle_message(ChatRequest(message="Save the first one", session_id=session))
    saved_record = saved.tool.data["saved"][0]
    repeated = handle_message(ChatRequest(message="Save the first one", session_id=session))
    assert repeated.tool.data["already_saved"] == 1
    assert len(OpportunityService(main.db).list_saved()) == 1

    note = handle_message(ChatRequest(message="Add a note to this internship: follow up next week", session_id=session))
    assert note.tool_status == "completed"
    changed = handle_message(ChatRequest(message="I applied to this one today", session_id=session))
    assert changed.tool.data["id"] == saved_record["id"]
    assert changed.tool.data["status"] == "APPLIED"
    history = OpportunityService(main.db).history(opportunity_id=saved_record["id"])
    assert history[0]["new_status"] == "APPLIED"
    assert history[0]["opportunity_id"] == saved_record["id"]
    stored = OpportunityService(main.db).get_saved(saved_record["id"])
    assert stored["notes"][0]["note"] == "follow up next week"

    refused = handle_message(ChatRequest(message="Save the first one", session_id="different-chat"))
    assert refused.tool_status == "error"
    workflow = main.workflow_service().active(session)
    assert workflow["memory"]["selected_opportunity_ids"] == [saved_record["id"]]
    assert main.state(session)["agent_workflows"][0]["id"] == workflow["id"]
    assert main.state("different-chat")["agent_workflows"] == []


def test_journey_resume_review_adds_versioned_fact_without_sharing_permission(integration_db):
    main.start()
    service = ResumeService(main.db)
    uploaded = service.upload("candidate.docx", _resume_docx())
    github = next(item for item in uploaded["items"] if item["label"] == "GitHub")
    accepted = service.approve_items(uploaded["resume"]["id"], [github["id"]])["items"][0]
    assert accepted["review_status"] == "USER_VERIFIED"
    assert service.approve_items(uploaded["resume"]["id"], [github["id"]])["items"][0]["passport_version"] == accepted["passport_version"]
    with main.db() as connection:
        fact = connection.execute("SELECT * FROM facts WHERE id='github_url' ORDER BY version DESC LIMIT 1").fetchone()
        assert fact["source_document_id"] == uploaded["resume"]["id"]
        assert fact["source_type"] == "resume"
        assert fact["permitted_origins"] == "[]"
        assert fact["user_confirmed"] == 1
    assert service.delete_resume(uploaded["resume"]["id"])
    with main.db() as connection:
        assert connection.execute("SELECT count(*) FROM resume_items WHERE resume_id=?", (uploaded["resume"]["id"],)).fetchone()[0] == 0
        retained = connection.execute("SELECT * FROM facts WHERE id='github_url'").fetchone()
        assert retained["value"] == "https://github.com/kaiexample"
        assert retained["source_document_id"] == uploaded["resume"]["id"]
