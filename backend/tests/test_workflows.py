from types import SimpleNamespace

import pytest
from backend.app import main
from backend.app.workflows import WorkflowError
from backend.app.workflows.schemas import WorkflowCreate


@pytest.fixture
def clean_workflow_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", tmp_path / "workflows.db")
    main.init()
    return main.workflow_service()


def test_workflow_create_and_checkpoint_are_persistent_and_deduplicated(clean_workflow_db):
    service = clean_workflow_db
    created = service.create("Find paid AI internships in India", "session-1")
    assert created["status"] == "RUNNING"
    assert created["plan"][0]["status"] == "completed"

    result = SimpleNamespace(summary="Found 2 supported listings.", data={"opportunities": [{"id": "listing-1"}, {"id": "listing-2"}], "query_summary": {"keywords": "AI"}})
    service.record_tool("session-1", "search_internships", result)
    service.record_tool("session-1", "search_internships", result)
    restored = service.get(created["id"])
    assert restored["status"] == "WAITING_FOR_USER"
    assert restored["memory"]["search_criteria"] == {"keywords": "AI"}
    assert len([x for x in restored["checkpoints"] if x["step"] == "search_internships"]) == 1


def test_lifecycle_validates_pause_resume_and_cancel(clean_workflow_db):
    service = clean_workflow_db
    item = service.create("Prepare applications for saved internships")
    assert service.transition(item["id"], "pause")["status"] == "PAUSED"
    assert service.transition(item["id"], "resume")["status"] == "RUNNING"
    assert service.transition(item["id"], "complete")["status"] == "COMPLETED"
    with pytest.raises(WorkflowError):
        service.transition(item["id"], "resume")


def test_pending_decision_is_persisted_then_resolved(clean_workflow_db):
    service = clean_workflow_db
    item = service.create("Find and shortlist internships")
    waiting = service.ask(item["id"], "Which listings should I save?")
    decision = waiting["decisions"][0]
    assert waiting["status"] == "WAITING_FOR_USER"
    resolved = service.respond(item["id"], decision["id"], "Save listing 2")
    assert resolved["status"] == "RUNNING"
    assert resolved["memory"]["pending_decision"] == "Save listing 2"
    assert resolved["decisions"] == []


def test_workflow_api_and_chat_pause_resume(clean_workflow_db):
    created = main.create_agent_workflow(WorkflowCreate(goal="Find software internships", session_id="api-chat"))
    wid = created["id"]
    from backend.app.conversation import ChatRequest, handle_message
    assert "paused" in handle_message(ChatRequest(message="Pause this workflow", session_id="api-chat")).assistant_message
    assert handle_message(ChatRequest(message="What's my current status?", session_id="api-chat")).tool_status == "completed"
    assert main.get_agent_workflow_checkpoints(wid, "api-chat")["checkpoints"]
    assert "running" in handle_message(ChatRequest(message="Resume this workflow", session_id="api-chat")).assistant_message
    assert main.list_agent_workflows("api-chat")["workflows"][0]["status"] == "RUNNING"


def test_failed_step_is_marked_partial_for_inspection(clean_workflow_db):
    service = clean_workflow_db
    item = service.create("Search for data internships")
    service.record_tool("default", "search_internships", None, "temporarily unavailable")
    restored = service.get(item["id"])
    assert restored["status"] == "PARTIALLY_COMPLETED"
    assert restored["recovery_state"] == "Inspect existing records before retrying."
    assert restored["checkpoints"][-1]["status"] == "failed"


def test_cancelled_workflow_cannot_be_revived_by_stale_decision(clean_workflow_db):
    service = clean_workflow_db
    item = service.create("Select appropriate AI internships")
    waiting = service.ask(item["id"], "Which result should I save?")
    decision = waiting["decisions"][0]
    service.transition(item["id"], "cancel")
    with pytest.raises(WorkflowError, match="no longer waiting"):
        service.respond(item["id"], decision["id"], "The first result")
    assert service.get(item["id"])["status"] == "CANCELLED"


def test_decision_request_is_idempotent_and_closed_on_resume(clean_workflow_db):
    service = clean_workflow_db
    item = service.create("Choose internships to save")
    first = service.ask(item["id"], "Which one should I save?")
    duplicate = service.ask(item["id"], "Which one should I save?")
    assert first["decisions"][0]["id"] == duplicate["decisions"][0]["id"]
    service.transition(item["id"], "resume")
    assert service.get(item["id"])["decisions"] == []


def test_workflow_api_is_scoped_to_its_chat_session(clean_workflow_db):
    from fastapi import HTTPException

    item = clean_workflow_db.create("Find data internships", "chat-one")
    with pytest.raises(WorkflowError, match="not found"):
        clean_workflow_db.get_owned(item["id"], "chat-two")
    with pytest.raises(HTTPException) as error:
        main.get_agent_workflow(item["id"], "chat-two")
    assert error.value.status_code == 404
