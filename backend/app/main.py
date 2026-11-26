from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load configuration from .env if present
load_dotenv()

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi import File, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.app.automation import FormAutomator, get_playwright_manager
from backend.app.conversation import ChatRequest, ChatResponse, handle_message
from backend.app.opportunities.schemas import (
    ApplicationStatus, NoteCreateRequest, NoteUpdateRequest, OpportunityNote, OperationResult,
    SaveOpportunityRequest, SavedOpportunityList, SavedOpportunityRecord, SavedOpportunityResult,
    StatusHistoryList, StatusUpdateRequest,
)
from backend.app.opportunities.service import OpportunityNotFound, OpportunityService, init_schema as init_opportunity_schema
from backend.app.preparation.schemas import (
    FieldReviewRequest, OpportunityPreview, OriginApproval, OriginApprovalRequest,
    PreparationApprovalRequest, PreparationList, PreparationReceipt, PreparationResult,
    PreparationStartRequest,
)
from backend.app.preparation.security import UnsafeDestination, extract_https_origin, validate_public_https_url
from backend.app.preparation.service import PreparationError, PreparationService, init_schema as init_preparation_schema
from backend.app.resumes.schemas import (
    ResumeDeleteResult, ResumeDetail, ResumeItem, ResumeItemUpdate, ResumeList,
    ResumeReviewRequest, ResumeReviewResult, ResumeUploadResult,
)
from backend.app.resumes.service import ResumeInputError, ResumeService, init_schema as init_resume_schema
from backend.app.workflows import WorkflowError, WorkflowService, init_schema as init_workflow_schema
from backend.app.workflows.schemas import WorkflowCreate, WorkflowDecision

from backend.app.translator import (
    CANONICAL_FIELDS,
    FieldTranslator,
    MappingResult,
    get_translator,
)
from backend.app.passport import (
    FactCreateBody,
    FactUpdateBody,
    FactConfirmBody,
    FactPermissionsBody,
    list_passport_facts,
    add_or_version_fact,
    update_fact_value,
    confirm_fact_version,
    update_fact_permissions,
)

ROOT = Path(__file__).resolve().parents[2]
DB = Path(os.getenv("CARRYFLOW_DB", str(ROOT / "carryflow.db")))
NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
ALLOWED = {"http://127.0.0.1:8000", "http://localhost:8000"}


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    init()
    yield
    try:
        mgr = get_playwright_manager()
        if mgr.is_running:
            await mgr.stop()
    except Exception:
        pass


app = FastAPI(title="CarryFlow", version="0.1.0", lifespan=lifespan)


class ResolveBody(BaseModel):
    fact_id: str
    version: int


class StartBody(BaseModel):
    user_goal: str = "Prepare two internship applications using my profile. Do not submit anything without asking."


class FillBody(BaseModel):
    origin: str
    observed: dict[str, str] = Field(default_factory=dict)


class TranslateBody(BaseModel):
    label: str
    description: str | None = None


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS facts(id TEXT, key TEXT, semantic_type TEXT, value TEXT, source_url TEXT, source_label TEXT, collected_at TEXT, version INTEGER, confidence REAL, verification TEXT, permitted_origins TEXT, purpose TEXT, user_confirmed INTEGER, last_confirmed_at TEXT, PRIMARY KEY(id,version));
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, kind TEXT, title TEXT, detail TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, origin TEXT, source_fact_id TEXT, source_version INTEGER, source_field TEXT, destination_field TEXT, semantic_type TEXT, mapping_type TEXT, reason TEXT, confidence REAL, permission TEXT, status TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE, origin TEXT, action_type TEXT, payload_fingerprint TEXT, status TEXT, observed_result TEXT, verification TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY, fact_id TEXT, old_version INTEGER, new_version INTEGER, resolved INTEGER DEFAULT 0, resolution_version INTEGER);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        """)
        init_opportunity_schema(c)
        init_resume_schema(c)
        init_preparation_schema(c)
        init_workflow_schema(c)
        # Upgrade schema if needed
        info = c.execute("PRAGMA table_info(facts)").fetchall()
        if not next((col[5] for col in info if col[1] == "version"), 0):
            c.execute("ALTER TABLE facts RENAME TO facts_legacy")
            c.execute("CREATE TABLE facts(id TEXT, key TEXT, semantic_type TEXT, value TEXT, source_url TEXT, source_label TEXT, collected_at TEXT, version INTEGER, confidence REAL, verification TEXT, permitted_origins TEXT, purpose TEXT, user_confirmed INTEGER, last_confirmed_at TEXT, PRIMARY KEY(id,version))")
            c.execute("INSERT INTO facts(id, key, semantic_type, value, source_url, source_label, collected_at, version, confidence, verification, permitted_origins, purpose, user_confirmed) SELECT * FROM facts_legacy")
            c.execute("DROP TABLE facts_legacy")
        fact_columns = {col[1] for col in c.execute("PRAGMA table_info(facts)")}
        if "last_confirmed_at" not in fact_columns:
            c.execute("ALTER TABLE facts ADD COLUMN last_confirmed_at TEXT")
        action_columns = {col[1] for col in c.execute("PRAGMA table_info(actions)")}
        if "preconditions" not in action_columns:
            c.execute("ALTER TABLE actions ADD COLUMN preconditions TEXT NOT NULL DEFAULT '{}'")
        fact_columns = {col[1] for col in c.execute("PRAGMA table_info(facts)")}
        fact_additions = {
            "source_type": "TEXT NOT NULL DEFAULT 'manual'",
            "source_document_id": "TEXT",
            "source_section": "TEXT",
            "source_evidence": "TEXT",
            "extraction_confidence": "REAL",
            "extracted_at": "TEXT",
        }
        for column, definition in fact_additions.items():
            if column not in fact_columns:
                c.execute(f"ALTER TABLE facts ADD COLUMN {column} {definition}")
        c.execute("INSERT OR IGNORE INTO meta VALUES('workflow_id',?)", (str(uuid.uuid4()),))
        c.execute("INSERT OR IGNORE INTO meta VALUES('status','PENDING')")
        c.execute("INSERT OR IGNORE INTO meta VALUES('current_origin','http://127.0.0.1:8000/portal/source')")
        c.execute("INSERT OR IGNORE INTO meta VALUES('user_goal',?)", (StartBody().user_goal,))


def startup() -> None:
    init()


def event(c: sqlite3.Connection, kind: str, title: str, detail: str = "") -> None:
    c.execute("INSERT INTO events VALUES(?,?,?,?,?)", (str(uuid.uuid4()), kind, title, detail, NOW()))


FACTS = [
    ("full_name", "person.name", "Aarav Mehta", "Full name", .99),
    ("email", "person.email", "aarav.mehta@example.test", "Email address", .99),
    ("phone", "person.phone", "9876543210", "Phone number", .99),
    ("education", "education.degree", "B.Tech Information Technology", "Degree", .97),
    ("graduation_year", "education.graduation_year", "2027", "Graduation year", .98),
    ("skills", "skills.technical", "Python, React, SQL", "Technical skills", .96),
    ("current_address", "address.current", "12 Lake View Road, Bengaluru", "Current address", .96),
    ("permanent_address", "address.permanent", "44 Park Street, Mysuru", "Permanent address", .96),
]
PORTALS = {
    "a": {"name": "Northstar Internships", "url": "http://127.0.0.1:8000/portal/a", "fields": [
        ("candidate_name", "Candidate name", "person.name"), ("academic_background", "Academic background", "education.degree"), ("expected_completion", "Expected completion", "education.graduation_year"), ("technical_competencies", "Technical competencies", "skills.technical"), ("contact_email", "Contact email", "person.email"), ("phone", "Phone number", "person.phone")]},
    "b": {"name": "Summit Talent", "url": "http://127.0.0.1:8000/portal/b", "fields": [
        ("applicant_name", "Applicant name", "person.name"), ("highest_qualification", "Highest qualification", "education.degree"), ("passing_year", "Passing year", "education.graduation_year"), ("core_competencies", "Core competencies", "skills.technical"), ("current_location", "Current location", "address.current"), ("location", "Location", "address.unknown")]}
}


def get_current_facts(c: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Return the active fact for each semantic type, honoring conflict resolutions."""
    resolved = {
        row["fact_id"]: row["resolution_version"]
        for row in c.execute("SELECT fact_id, resolution_version FROM conflicts WHERE resolved=1 ORDER BY rowid ASC").fetchall()
    }
    all_facts = c.execute("SELECT * FROM facts ORDER BY id, version DESC").fetchall()
    by_id: dict[str, list[sqlite3.Row]] = {}
    for f in all_facts:
        by_id.setdefault(f["id"], []).append(f)
    current: dict[str, sqlite3.Row] = {}
    for fid, f_list in by_id.items():
        chosen = None
        if fid in resolved and resolved[fid] is not None:
            chosen = next((f for f in f_list if f["version"] == resolved[fid]), None)
        if not chosen:
            non_superseded = [f for f in f_list if f["verification"] != "SUPERSEDED"]
            chosen = non_superseded[0] if non_superseded else f_list[0]
        if chosen:
            current[chosen["semantic_type"]] = chosen
    return current


app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


@app.get("/portal/{which}", response_class=HTMLResponse)
def portal(which: str) -> str:
    if which not in {"source", "a", "b"}:
        raise HTTPException(404)
    if which == "source":
        fields = [(k, label, val) for k, label, val, _, _ in FACTS]
        title, sub = "Student profile", "Your information, ready to carry forward"
    else:
        p = PORTALS[which]
        fields = [(k, label, "") for k, label, _ in p["fields"]]
        title, sub = p["name"], "Internship application · Draft"
    html = [f'<main class="portal portal-{which}"><div class="pbrand"><span>✳</span> {"CARRYFLOW PROFILE" if which=="source" else "CAREERS / INTERNSHIPS"}</div><h1>{title}</h1><p>{sub}</p><section class="pform">']
    for k, label, val in fields:
        html.append(f'<label for="{k}">{label}<input id="{k}" name="{k}" value="{val}" autocomplete="off"></label>')
    html.append('<div class="pfoot"><span id="save-note">Draft saved locally</span><button type="button" id="submit-application">Submit application</button></div></section><p class="safe-note">Demo site · Synthetic student data · Submission stays under your control</p></main>')
    html.append('<script>document.querySelectorAll("input").forEach(i=>i.addEventListener("input",()=>{document.querySelector("#save-note").textContent="Unsaved changes"}));document.querySelector("#submit-application").onclick=()=>{document.querySelector("#save-note").textContent="Submission requires CarryFlow approval"};</script>')
    return "<!doctype html><meta charset=utf-8><link rel=stylesheet href=/static/portal.css><link rel=stylesheet href=/static/portal-variants.css>" + "".join(html)


@app.get("/api/state")
def state(session_id: str | None = None) -> dict[str, Any]:
    with db() as c:
        facts = [dict(r) for r in c.execute("SELECT * FROM facts ORDER BY key,version")]
        events = [dict(r) for r in c.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 40")]
        receipts = [dict(r) for r in c.execute("SELECT * FROM receipts ORDER BY created_at DESC")]
        actions = [dict(r) for r in c.execute("SELECT * FROM actions ORDER BY created_at DESC")]
        conflicts = [dict(r) for r in c.execute("SELECT * FROM conflicts WHERE resolved=0")]
        meta = dict(c.execute("SELECT key,value FROM meta"))
    return {"facts": facts, "events": events, "receipts": receipts, "actions": actions, "conflicts": conflicts, "meta": meta, "portals": PORTALS,
            "preparations": preparation_service().list(), "agent_workflows": WorkflowService(db).list(session_id)[:10] if session_id else []}


# --- Smart Personal Data Passport Endpoints ---

@app.get("/api/passport")
def get_passport() -> dict[str, Any]:
    with db() as c:
        return list_passport_facts(c)


@app.post("/api/passport/fact")
def create_passport_fact(body: FactCreateBody) -> dict[str, Any]:
    with db() as c:
        res = add_or_version_fact(c, body)
        event(c, "passport", f"Fact '{body.label}' updated", f"Version {res['version']} recorded with status {res['verification']}.")
        return res


@app.put("/api/passport/fact/{fact_id}")
def update_passport_fact(fact_id: str, body: FactUpdateBody) -> dict[str, Any]:
    with db() as c:
        res = update_fact_value(c, fact_id, body)
        event(c, "passport", f"Fact '{fact_id}' edited", f"Action: {res['action']} (v{res['version']}).")
        return res


@app.post("/api/passport/fact/{fact_id}/confirm")
def confirm_passport_fact(fact_id: str, body: FactConfirmBody | None = None) -> dict[str, Any]:
    with db() as c:
        target_version = body.version if body else None
        res = confirm_fact_version(c, fact_id, target_version)
        event(c, "approval", f"Fact '{fact_id}' confirmed", f"User confirmed version {res['version']}.")
        return res


@app.post("/api/passport/fact/{fact_id}/permissions")
def update_passport_permissions(fact_id: str, body: FactPermissionsBody) -> dict[str, Any]:
    checked_origins = []
    for value in body.permitted_origins:
        if value in ALLOWED:
            checked_origins.append(value)
            continue
        try:
            origin, normalized = validate_public_https_url(value.rstrip("/") + "/")
        except UnsafeDestination as exc:
            raise HTTPException(422, f"Invalid sharing origin: {exc}") from exc
        if normalized != origin + "/" or value.rstrip("/").lower() != origin:
            raise HTTPException(422, "Sharing permissions must contain exact HTTPS origins without paths.")
        checked_origins.append(origin)
    body.permitted_origins = sorted(set(checked_origins))
    with db() as c:
        res = update_fact_permissions(c, fact_id, body)
        event(c, "passport", f"Permissions updated for '{fact_id}'", f"Origins allowed: {', '.join(body.permitted_origins)}.")
        return res


@app.post("/api/reset")
def reset() -> dict[str, bool]:
    with db() as c:
        c.execute("UPDATE resume_items SET review_status='EXTRACTED',passport_fact_id=NULL,passport_version=NULL,review_error=NULL WHERE review_status IN ('USER_VERIFIED','CONFLICT_PENDING')")
        for t in ("facts", "events", "receipts", "actions", "conflicts"):
            c.execute(f"DELETE FROM {t}")
        c.execute("UPDATE meta SET value=? WHERE key='workflow_id'", (str(uuid.uuid4()),))
        c.execute("UPDATE meta SET value='PENDING' WHERE key='status'")
        c.execute("UPDATE meta SET value=? WHERE key='current_origin'", (PORTALS["a"]["url"],))
        event(c, "system", "Demo reset", "Synthetic data cleared.")
    return {"ok": True}


@app.post("/api/start")
def start(body: StartBody | None = None) -> dict[str, Any]:
    goal = body.user_goal.strip() if body and body.user_goal.strip() else StartBody().user_goal
    with db() as c:
        if c.execute("SELECT count(*) FROM facts").fetchone()[0] == 0:
            now_iso = NOW()
            for key, typ, value, label, conf in FACTS:
                c.execute(
                    "INSERT INTO facts(id, key, semantic_type, value, source_url, source_label, collected_at, version, confidence, verification, permitted_origins, purpose, user_confirmed, last_confirmed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (key, key, typ, value, PORTALS["a"]["url"].replace("/a", "/source"), label, now_iso, 1, conf, "VERIFIED", json.dumps(sorted(ALLOWED)), "internship application preparation", 1, now_iso)
                )
            event(c, "extract", "Profile facts verified", "8 synthetic facts recorded with source labels and permission scope.")
        c.execute("UPDATE meta SET value='RUNNING' WHERE key='status'")
        c.execute("UPDATE meta SET value=? WHERE key='user_goal'", (goal,))
        c.execute("UPDATE meta SET value=? WHERE key='current_origin'", (PORTALS["a"]["url"],))
        event(c, "checkpoint", "Workflow started", "Two local internship portals are in scope. Submissions require approval.")
    return state()


def semantic_for(label: str) -> str:
    """Intelligent semantic mapping for a field label."""
    res = get_translator().translate(label)
    return res.semantic_type or ("address.unknown" if "location" in label.casefold() else "unknown")


@app.post("/api/translate", response_model=MappingResult)
def translate_field(body: TranslateBody) -> MappingResult:
    """Direct field translation endpoint using Intelligent Field Translator."""
    return get_translator().translate(label=body.label, description=body.description)


@app.post("/api/chat", response_model=ChatResponse)
def chat(body: ChatRequest) -> ChatResponse:
    """Interpret a supported conversational request and run a policy-checked tool."""
    return handle_message(body)


def workflow_service() -> WorkflowService:
    return WorkflowService(db)


@app.post("/api/workflows")
def create_agent_workflow(body: WorkflowCreate) -> dict[str, Any]:
    try:
        return workflow_service().create(body.goal, body.session_id)
    except WorkflowError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/workflows")
def list_agent_workflows(session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    return {"workflows": workflow_service().list(session_id)}


@app.get("/api/workflows/{workflow_id}")
def get_agent_workflow(workflow_id: str, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    try:
        return workflow_service().get_owned(workflow_id, session_id)
    except WorkflowError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/workflows/{workflow_id}/history")
def get_agent_workflow_history(workflow_id: str, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    item = get_agent_workflow(workflow_id, session_id)
    return {"activity": item["activity"]}


@app.get("/api/workflows/{workflow_id}/activity")
def get_agent_workflow_activity(workflow_id: str, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    return get_agent_workflow_history(workflow_id, session_id)


@app.get("/api/workflows/{workflow_id}/checkpoints")
def get_agent_workflow_checkpoints(workflow_id: str, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    item = get_agent_workflow(workflow_id, session_id)
    return {"checkpoints": item["checkpoints"]}


@app.get("/api/workflows/{workflow_id}/next-step")
def get_agent_workflow_next_step(workflow_id: str, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    item = get_agent_workflow(workflow_id, session_id)
    action = item["pending_action"] or ("Continue with the next supported workflow step." if item["status"] in {"RUNNING", "PARTIALLY_COMPLETED"} else "Wait for your decision or resume this workflow.")
    return {"workflow_id": workflow_id, "status": item["status"], "recommendation": action}


@app.get("/api/workflow-approvals/pending")
def get_pending_workflow_approvals(session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    return {"workflows": [w for w in workflow_service().list(session_id) if w["status"] == "WAITING_FOR_APPROVAL"]}


@app.post("/api/workflows/{workflow_id}/{action}")
def transition_agent_workflow(workflow_id: str, action: str, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    try:
        workflow_service().get_owned(workflow_id, session_id)
        return workflow_service().transition(workflow_id, action)
    except WorkflowError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/workflows/{workflow_id}/decisions")
def ask_agent_workflow_decision(workflow_id: str, body: WorkflowDecision, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    try:
        workflow_service().get_owned(workflow_id, session_id)
        return workflow_service().ask(workflow_id, body.decision)
    except WorkflowError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/workflows/{workflow_id}/decisions/{decision_id}/respond")
def respond_agent_workflow_decision(workflow_id: str, decision_id: str, body: WorkflowDecision, session_id: str = Query(min_length=1, max_length=64)) -> dict[str, Any]:
    try:
        workflow_service().get_owned(workflow_id, session_id)
        return workflow_service().respond(workflow_id, decision_id, body.decision)
    except WorkflowError as exc:
        raise HTTPException(409, str(exc)) from exc


def opportunity_service() -> OpportunityService:
    return OpportunityService(db)


def resume_service() -> ResumeService:
    return ResumeService(db)


def preparation_service() -> PreparationService:
    return PreparationService(db)


def _preparation_facts() -> list[dict[str, Any]]:
    with db() as c:
        return [dict(row) for row in get_current_facts(c).values()]


def _revalidate_preparation_opportunity(preparation: dict[str, Any]) -> None:
    try:
        saved = opportunity_service().get_saved(preparation["opportunity_id"])
        origin, destination = validate_public_https_url(saved["opportunity"]["listing_url"])
    except (OpportunityNotFound, UnsafeDestination, ValueError) as exc:
        raise PreparationError("The saved opportunity is no longer available or its destination is invalid. Save and review it again before continuing.", 409) from exc
    if origin != preparation["origin"] or destination != preparation["destination_url"]:
        raise PreparationError("The saved opportunity destination changed. Preview it and start a new preparation review.", 409)


@app.get("/api/preparations/origins")
def list_preparation_origins() -> dict[str, Any]:
    return {"origins": preparation_service().approved_origins()}


@app.post("/api/preparations/origins/approve", response_model=OriginApproval)
def approve_preparation_origin(body: OriginApprovalRequest) -> dict[str, Any]:
    try:
        origin, normalized = validate_public_https_url(body.origin)
    except UnsafeDestination as exc:
        raise HTTPException(422, str(exc)) from exc
    if normalized.rstrip("/") != origin or body.origin.rstrip("/").lower() != origin:
        raise HTTPException(422, "Approve the exact HTTPS origin only; paths and URL credentials are not allowed here.")
    saved = preparation_service().approve_origin(origin)
    get_playwright_manager().approve_origin(origin)
    with db() as c:
        event(c, "origin_approval", "Application origin approved", f"User explicitly approved {origin} for controlled application preparation.")
    return saved


@app.get("/api/preparations/preview/{opportunity_id}", response_model=OpportunityPreview)
def preview_preparation(opportunity_id: str) -> dict[str, Any]:
    try:
        record = opportunity_service().get_saved(opportunity_id)
        origin, destination_url = validate_public_https_url(record["opportunity"]["listing_url"])
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (ValueError, UnsafeDestination) as exc:
        raise HTTPException(422, str(exc)) from exc
    item = record["opportunity"]
    return {"opportunity_id": record["id"], "title": item["title"], "company_name": item.get("company_name"),
            "destination_url": destination_url, "origin": origin, "origin_approved": preparation_service().origin_is_approved(origin)}


@app.post("/api/preparations/start", response_model=PreparationResult)
async def start_preparation(body: PreparationStartRequest) -> dict[str, Any]:
    try:
        record = opportunity_service().get_saved(body.opportunity_id)
        origin, destination_url = validate_public_https_url(record["opportunity"]["listing_url"])
        workflow = preparation_service().create(record["id"], record["opportunity"], destination_url, origin)
        manager = get_playwright_manager()
        manager.approve_origin(origin)
        nav = await manager.navigate(destination_url)
        if not nav.get("ok") or nav.get("url", "").split("/", 3)[:3] != destination_url.split("/", 3)[:3]:
            raise PreparationError("The application page did not remain on its approved origin.", 403)
        inspected = await FormAutomator.inspect_fields(await manager.get_page())
        result = preparation_service().set_inspection(workflow["id"], inspected["fields"], _preparation_facts(), get_translator(), origin, excluded_count=inspected["excluded_count"])
        return {"preparation": result}
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (UnsafeDestination, PreparationError) as exc:
        raise HTTPException(getattr(exc, "status_code", 422), str(exc)) from exc
    except Exception as exc:
        if "workflow" in locals():
            preparation_service().fail(workflow["id"], "Browser navigation or inspection failed; no fields were prepared.")
        raise HTTPException(502, "Could not safely open or inspect the application page.") from exc


@app.get("/api/preparations", response_model=PreparationList)
def list_preparations() -> dict[str, Any]:
    return {"preparations": preparation_service().list()}


@app.get("/api/preparations/{preparation_id}", response_model=PreparationResult)
def get_preparation(preparation_id: str) -> dict[str, Any]:
    try:
        return {"preparation": preparation_service().get(preparation_id)}
    except PreparationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.put("/api/preparations/{preparation_id}/fields/{field_id}/review", response_model=PreparationResult)
def review_preparation_field(preparation_id: str, field_id: str, body: FieldReviewRequest) -> dict[str, Any]:
    try:
        result = preparation_service().review_field(preparation_id, field_id, body.decision, body.fact_id, _preparation_facts(), preparation_service().get(preparation_id)["origin"])
        return {"preparation": result}
    except PreparationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/api/preparations/{preparation_id}/approve", response_model=PreparationResult)
def approve_preparation(preparation_id: str, body: PreparationApprovalRequest) -> dict[str, Any]:
    try:
        return {"preparation": preparation_service().approve(preparation_id, body.approved, _preparation_facts())}
    except PreparationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/api/preparations/{preparation_id}/fill", response_model=PreparationResult)
async def fill_preparation(preparation_id: str) -> dict[str, Any]:
    service = preparation_service()
    manager = get_playwright_manager()
    try:
        state = service.get(preparation_id)
        _revalidate_preparation_opportunity(state)
        if not service.origin_is_approved(state["origin"]):
            raise PreparationError("Destination origin is no longer approved.", 403)
        page = await manager.get_page()
        inspected = await FormAutomator.inspect_fields(page)
        current_facts = _preparation_facts()
        state, values, approved = service.fill_plan(preparation_id, page.url, inspected["fields"], current_facts)
        manager.lock_page_egress()
        outcome = await FormAutomator.fill_fields(page, values, {field_id: field["fingerprint"] for field_id, field in approved.items()})
        names = {field_id: field["label"] for field_id, field in approved.items()}
        receipt = {
            "id": str(uuid.uuid4()), "workflow_id": state["id"], "opportunity_id": state["opportunity_id"],
            "origin": state["origin"], "prepared_at": NOW(), "detected_field_count": len(state["fields"]),
            "approved_mapping_count": len(approved), "successfully_filled_fields": [names[key] for key in outcome["filled"] if key in names],
            "skipped_fields": [field["label"] for field in state["fields"] if field["mapping_status"] != "APPROVED"],
            "permission_blocked_fields": [field["label"] for field in state["fields"] if field["mapping_status"] == "PERMISSION_BLOCKED"],
            "ambiguous_fields": [field["label"] for field in state["fields"] if field["mapping_status"] == "AMBIGUOUS"],
            "failed_fields": {names.get(key, key): value for key, value in outcome["errors"].items()},
            "recovery_state": "PARTIALLY_PREPARED" if outcome["errors"] else "PREPARED",
            "user_approval_reference": state.get("approval_reference"),
        }
        try:
            await manager.capture_screenshot()
        except Exception:
            pass
        status = "PARTIALLY_PREPARED" if outcome["errors"] else "PREPARED"
        result = service.save_receipt(preparation_id, receipt, status)
        with db() as c:
            event(c, "handoff", "Application preparation receipt created", f"{len(receipt['successfully_filled_fields'])} field(s) prepared; {len(receipt['failed_fields'])} failed. Application was not submitted.")
        return {"preparation": result}
    except PreparationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "The browser could not complete preparation safely.") from exc


@app.get("/api/preparations/{preparation_id}/receipt", response_model=PreparationReceipt)
def get_preparation_receipt(preparation_id: str) -> dict[str, Any]:
    try:
        receipt = preparation_service().get(preparation_id).get("receipt")
        if not receipt:
            raise PreparationError("A handoff receipt is not available until field preparation has run.", 404)
        return receipt
    except PreparationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@app.post("/api/preparations/{preparation_id}/recover", response_model=PreparationResult)
async def recover_preparation(preparation_id: str) -> dict[str, Any]:
    service = preparation_service()
    try:
        state = service.get(preparation_id)
        _revalidate_preparation_opportunity(state)
        if not service.origin_is_approved(state["origin"]):
            raise PreparationError("Destination origin must be reapproved before recovery.", 403)
        manager = get_playwright_manager()
        manager.approve_origin(state["origin"])
        await manager.navigate(state["destination_url"])
        inspected = await FormAutomator.inspect_fields(await manager.get_page())
        result = service.set_inspection(preparation_id, inspected["fields"], _preparation_facts(), get_translator(), state["origin"], recovery=True, excluded_count=inspected["excluded_count"])
        return {"preparation": result}
    except PreparationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, "Recovery could not safely reopen and inspect the approved page.") from exc


@app.post("/api/resumes/upload", response_model=ResumeUploadResult)
async def upload_resume(file: UploadFile = File(...)) -> dict[str, Any]:
    try:
        content = await file.read(8 * 1024 * 1024 + 1)
        if len(content) > 8 * 1024 * 1024:
            raise ResumeInputError("Resume files must be 8 MB or smaller.")
        return resume_service().upload(file.filename, content)
    except ResumeInputError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/resumes", response_model=ResumeList)
def list_resumes() -> dict[str, Any]:
    return {"resumes": resume_service().list_resumes()}


@app.get("/api/resumes/{resume_id}", response_model=ResumeDetail)
def get_resume(resume_id: str) -> dict[str, Any]:
    return resume_service().get_resume(resume_id)


@app.put("/api/resumes/{resume_id}/items/{item_id}", response_model=ResumeItem)
def update_resume_item(resume_id: str, item_id: str, body: ResumeItemUpdate) -> dict[str, Any]:
    return resume_service().update_item(resume_id, item_id, body)


@app.post("/api/resumes/{resume_id}/approve", response_model=ResumeReviewResult)
def approve_resume_items(resume_id: str, body: ResumeReviewRequest) -> dict[str, Any]:
    result = resume_service().approve_items(resume_id, body.item_ids)
    if any(item.get("review_status") in {"USER_VERIFIED", "CONFLICT_PENDING"} for item in result["items"]):
        with db() as c:
            event(c, "passport", "Resume facts reviewed", f"{sum(item.get('review_status') == 'USER_VERIFIED' for item in result['items'])} fact(s) verified; conflicts remain for explicit resolution.")
    return result


@app.post("/api/resumes/{resume_id}/reject", response_model=ResumeReviewResult)
def reject_resume_items(resume_id: str, body: ResumeReviewRequest) -> dict[str, Any]:
    return resume_service().reject_items(resume_id, body.item_ids)


@app.delete("/api/resumes/{resume_id}", response_model=ResumeDeleteResult)
def delete_resume(resume_id: str) -> dict[str, Any]:
    deleted = resume_service().delete_resume(resume_id)
    if not deleted:
        raise HTTPException(404, "Resume not found")
    return {"deleted": True}


@app.post("/api/opportunities/save", response_model=SavedOpportunityResult)
def save_opportunity(body: SaveOpportunityRequest) -> dict[str, Any]:
    try:
        record, created = opportunity_service().save_from_context(body.session_id, body.opportunity_id)
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"opportunity": record, "created": created}


@app.get("/api/opportunities/saved", response_model=SavedOpportunityList)
def list_saved_opportunities(status: ApplicationStatus | None = None, sort_by_deadline: bool = False) -> dict[str, Any]:
    return {"opportunities": opportunity_service().list_saved(status, sort_by_deadline)}


@app.get("/api/opportunities/saved/{opportunity_id}", response_model=SavedOpportunityRecord)
def get_saved_opportunity(opportunity_id: str) -> dict[str, Any]:
    try:
        return opportunity_service().get_saved(opportunity_id)
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/opportunities/saved/{opportunity_id}", response_model=OperationResult)
def remove_saved_opportunity(opportunity_id: str) -> dict[str, Any]:
    try:
        removed = opportunity_service().remove(opportunity_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not removed:
        raise HTTPException(404, "Saved opportunity not found.")
    return {"removed": True}


@app.put("/api/opportunities/saved/{opportunity_id}/status", response_model=SavedOpportunityRecord)
def update_saved_opportunity_status(opportunity_id: str, body: StatusUpdateRequest) -> dict[str, Any]:
    try:
        return opportunity_service().update_status(opportunity_id, body.status)
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/opportunities/history", response_model=StatusHistoryList)
def get_opportunity_history(opportunity_id: str | None = None, company: str | None = None) -> dict[str, Any]:
    try:
        return {"history": opportunity_service().history(opportunity_id, company)}
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/opportunities/saved/{opportunity_id}/notes", response_model=OpportunityNote)
def create_opportunity_note(opportunity_id: str, body: NoteCreateRequest) -> dict[str, Any]:
    try:
        return opportunity_service().add_note(opportunity_id, body.note)
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/opportunities/saved/{opportunity_id}/notes/{note_id}", response_model=OpportunityNote)
def update_opportunity_note(opportunity_id: str, note_id: str, body: NoteUpdateRequest) -> dict[str, Any]:
    try:
        return opportunity_service().update_note(opportunity_id, note_id, body.note)
    except OpportunityNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/opportunities/saved/{opportunity_id}/notes/{note_id}", response_model=OperationResult)
def delete_opportunity_note(opportunity_id: str, note_id: str) -> dict[str, Any]:
    try:
        deleted = opportunity_service().delete_note(opportunity_id, note_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not deleted:
        raise HTTPException(404, "Note not found for this opportunity.")
    return {"deleted": True}


@app.post("/api/prepare/{portal_key}")
def prepare(portal_key: str) -> dict[str, Any]:
    if portal_key not in PORTALS:
        raise HTTPException(404)
    origin, fields = PORTALS[portal_key]["url"], PORTALS[portal_key]["fields"]
    destination_origin = origin.split("/portal/")[0]
    translator = get_translator()
    with db() as c:
        c.execute("UPDATE meta SET value=? WHERE key='current_origin'", (origin,))
        current_facts = get_current_facts(c)
        for field, label, declared in fields:
            mapping = translator.translate(label)
            sem = mapping.semantic_type or declared

            # Check for ambiguous fields or fields requiring user review
            if sem == "address.unknown" or mapping.canonical_field is None or mapping.requires_user_confirmation:
                c.execute(
                    "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        origin,
                        "",
                        0,
                        "Unresolved location" if sem == "address.unknown" else "Unresolved source",
                        field,
                        sem,
                        "AMBIGUOUS" if sem == "address.unknown" else "REQUIRES_USER",
                        mapping.explanation,
                        mapping.confidence,
                        "REQUIRES_USER",
                        "WAITING_FOR_USER",
                        NOW(),
                    ),
                )
                continue

            fact = current_facts.get(sem)
            if not fact:
                continue

            # USER CONTROL & SHARING CHECKS:
            # 1. Unconfirmed facts must NOT be automatically proposed/shared
            if not fact["user_confirmed"] or fact["verification"] in {"UNVERIFIED", "UNVERIFIED_CONFLICT"}:
                c.execute(
                    "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid.uuid4()),
                        origin,
                        fact["id"],
                        fact["version"],
                        fact["source_label"],
                        field,
                        sem,
                        "UNCONFIRMED_FACT",
                        f"'{fact['source_label']}' is not confirmed by the user. Confirmation required before sharing.",
                        fact["confidence"],
                        "REQUIRES_USER",
                        "WAITING_FOR_USER",
                        NOW(),
                    ),
                )
                continue

            # 2. Check origin sharing permission
            permitted = destination_origin in json.loads(fact["permitted_origins"])
            reason = f"'{label}' refers to {sem.replace('.', ' ')}; mapped from verified profile field '{fact['source_label']}'."
            status = "APPROVED" if permitted else "DENIED"
            c.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    origin,
                    fact["id"],
                    fact["version"],
                    fact["source_label"],
                    field,
                    sem,
                    "SEMANTIC_EQUIVALENT",
                    reason,
                    min(fact["confidence"], mapping.confidence),
                    status,
                    "PROPOSED" if permitted else "BLOCKED",
                    NOW(),
                ),
            )
        event(c, "handoff", "Handoff receipts prepared", f"{PORTALS[portal_key]['name']}: semantic mappings recorded; ambiguous fields held for review.")
    return state()


@app.post("/api/resolve")
def resolve(body: ResolveBody) -> dict[str, Any]:
    with db() as c:
        row = c.execute("SELECT * FROM facts WHERE id=? AND version=?", (body.fact_id, body.version)).fetchone()
        if not row:
            raise HTTPException(404, "Fact version not found")
        now_iso = NOW()
        c.execute("UPDATE facts SET verification='USER_CONFIRMED',user_confirmed=1,last_confirmed_at=? WHERE id=? AND version=?", (now_iso, body.fact_id, body.version))
        c.execute("UPDATE facts SET verification='SUPERSEDED' WHERE id=? AND version!=? AND verification='UNVERIFIED_CONFLICT'", (body.fact_id, body.version))
        c.execute("UPDATE conflicts SET resolved=1,resolution_version=? WHERE fact_id=? AND resolved=0", (body.version, body.fact_id))
        c.execute(
            "UPDATE resume_items SET review_status=CASE WHEN passport_version=? THEN 'USER_VERIFIED' ELSE 'REJECTED' END,review_error=NULL "
            "WHERE passport_fact_id=? AND review_status='CONFLICT_PENDING'",
            (body.version, body.fact_id),
        )
        event(c, "approval", "Conflict resolved", f"User selected {row['source_label']} version {body.version}; previous versions retained.")
    return state()


@app.post("/api/conflict")
def conflict() -> dict[str, Any]:
    with db() as c:
        row = c.execute("SELECT * FROM facts WHERE id='phone' ORDER BY version DESC LIMIT 1").fetchone()
        old = row["version"] if row else 0
        ver = old + 1
        now_iso = NOW()
        c.execute(
            "INSERT INTO facts(id, key, semantic_type, value, source_url, source_label, collected_at, version, confidence, verification, permitted_origins, purpose, user_confirmed, last_confirmed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("phone", "phone", "person.phone", "9123456780", PORTALS["b"]["url"], "Updated phone number", now_iso, ver, .88, "UNVERIFIED_CONFLICT", json.dumps(sorted(ALLOWED)), "internship application preparation", 0, None)
        )
        c.execute("INSERT INTO conflicts VALUES(?,?,?,?,0,NULL)", (str(uuid.uuid4()), "phone", old, ver))
        event(c, "conflict", "Conflicting phone number detected", "The latest profile visit differs from the saved value. Choose a version before reuse.")
    return state()


@app.post("/api/fill")
def fill(body: FillBody) -> dict[str, Any]:
    if body.origin not in {p["url"] for p in PORTALS.values()}:
        raise HTTPException(403, "Origin is outside the approved local demo scope")
    with db() as c:
        current = get_current_facts(c)
        for rec in c.execute("SELECT * FROM receipts WHERE origin=? AND status='PROPOSED'", (body.origin,)).fetchall():
            fact = current.get(rec["semantic_type"])
            if not fact or fact["verification"] in {"UNVERIFIED", "UNVERIFIED_CONFLICT"} or not fact["user_confirmed"]:
                continue
            key = f"{rec['origin']}|{rec['destination_field']}|{fact['id']}|{fact['version']}"
            fp = hashlib.sha256(fact["value"].encode("utf-8")).hexdigest()
            prior = c.execute("SELECT * FROM actions WHERE idempotency_key=?", (key,)).fetchone()
            observed = body.observed.get(rec["destination_field"])
            if prior and prior["status"] == "VERIFIED" and observed == fact["value"]:
                c.execute("UPDATE receipts SET status='VERIFIED' WHERE id=?", (rec["id"],))
                continue
            if prior and prior["status"] in {"PREPARED", "UNCERTAIN"}:
                c.execute("UPDATE actions SET status='UNCERTAIN',observed_result=? WHERE id=?", (observed or "not observed", prior["id"]))
                continue
            if rec["permission"] != "APPROVED":
                continue
            aid = str(uuid.uuid4())
            c.execute("INSERT OR IGNORE INTO actions(id,idempotency_key,origin,action_type,payload_fingerprint,status,observed_result,verification,created_at,preconditions) VALUES(?,?,?,?,?,?,?,?,?,?)", (aid, key, body.origin, "FILL_FIELD", fp, "PREPARED", "Prepared fill; waiting for verified field observation", "PENDING", NOW(), json.dumps({"receipt_id": rec["id"], "fact_version": fact["version"], "field_empty_or_matching": True})))
            if observed is not None:
                verified = observed == fact["value"]
                c.execute("UPDATE actions SET status=?,observed_result=?,verification=? WHERE idempotency_key=?", ("VERIFIED" if verified else "UNCERTAIN", "Field value observed" if verified else "Observed field does not match intended value", "VERIFIED" if verified else "UNCERTAIN", key))
                c.execute("UPDATE receipts SET status=? WHERE id=?", ("VERIFIED" if verified else "UNCERTAIN", rec["id"]))
        event(c, "action", "Page state recorded", "Observed form fields were reconciled with authorized mappings.")
    return state()


@app.post("/api/recover")
def recover(body: FillBody) -> dict[str, Any]:
    if body.origin not in {p["url"] for p in PORTALS.values()}:
        raise HTTPException(403, "Origin not approved")
    with db() as c:
        completed = 0
        uncertain = 0
        for a in c.execute("SELECT * FROM actions WHERE origin=?", (body.origin,)).fetchall():
            parts = a["idempotency_key"].split("|")
            field = parts[1] if len(parts) > 2 else ""
            observed = body.observed.get(field)
            if a["status"] == "VERIFIED" and observed:
                completed += 1
            elif a["status"] == "VERIFIED" and not observed:
                uncertain += 1
                c.execute("UPDATE actions SET status='UNCERTAIN',observed_result='Previously verified field is absent from the inspected page; user review required' WHERE id=?", (a["id"],))
            elif a["status"] in {"PREPARED", "UNCERTAIN"}:
                if observed:
                    uncertain += 1
                    c.execute("UPDATE actions SET status='UNCERTAIN',observed_result='Value exists after interruption; reconcile before any repeat' WHERE id=?", (a["id"],))
                else:
                    c.execute("UPDATE actions SET status='PENDING',observed_result='Field absent on inspected page; safe to prepare again' WHERE id=?", (a["id"],))
        event(c, "recovery", "Page inspected before resume", f"Observed {len(body.observed)} actual form values. {completed} verified actions already present; {uncertain} action(s) require reconciliation. No submission was replayed.")
        c.execute("UPDATE meta SET value='REQUIRES_RECONCILIATION' WHERE key='status'")
    return state()


@app.post("/api/submit/prepare")
def submit_prepare() -> dict[str, Any]:
    with db() as c:
        origin = c.execute("SELECT value FROM meta WHERE key='current_origin'").fetchone()[0]
        key = f"{origin}:FINAL_SUBMISSION:demo-application"
        existing = c.execute("SELECT * FROM actions WHERE idempotency_key=?", (key,)).fetchone()
        if not existing:
            c.execute("INSERT INTO actions(id,idempotency_key,origin,action_type,payload_fingerprint,status,observed_result,verification,created_at,preconditions) VALUES(?,?,?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), key, origin, "FINAL_SUBMISSION", "demo-application", "PREPARED", "Awaiting explicit user approval", "PENDING", NOW(), json.dumps({"explicit_user_approval_required": True})))
            event(c, "approval", "Submission prepared", "Review the website and approve explicitly. Preparation did not submit the application.")
    return state()


@app.post("/api/submit/approve")
def submit_approve() -> dict[str, Any]:
    with db() as c:
        row = c.execute("SELECT * FROM actions WHERE action_type='FINAL_SUBMISSION' ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            raise HTTPException(409, "Prepare submission first")
        if row["status"] != "PREPARED":
            raise HTTPException(409, "Submission outcome is not safe to retry")
        # Demo page is not actually submitted; this records approval and a deterministic confirmation.
        c.execute("UPDATE actions SET status='VERIFIED',observed_result='Approved locally; demo confirmation CF-DEMO-2048',verification='VERIFIED' WHERE id=?", (row["id"],))
        c.execute("UPDATE meta SET value='COMPLETED' WHERE key='status'")
        event(c, "verified", "Demo submission approved", "Confirmation CF-DEMO-2048 recorded; demo site did not send data externally.")
    return state()


# --- Playwright Real Browser Automation Endpoints ---

class AutomationNavigateBody(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


@app.get("/api/automation/status")
def automation_status() -> dict[str, Any]:
    """Return live status of Playwright browser."""
    return get_playwright_manager().get_status()


@app.post("/api/automation/start")
async def automation_start() -> dict[str, Any]:
    """Launch Playwright Chromium browser."""
    mgr = get_playwright_manager()
    started = await mgr.start()
    return {"ok": started, **mgr.get_status()}


@app.post("/api/automation/stop")
async def automation_stop() -> dict[str, Any]:
    """Stop Playwright Chromium browser."""
    mgr = get_playwright_manager()
    await mgr.stop()
    return {"ok": True, **mgr.get_status()}


@app.post("/api/automation/navigate")
async def automation_navigate(body: AutomationNavigateBody) -> dict[str, Any]:
    """Navigate Playwright browser to an approved portal URL."""
    mgr = get_playwright_manager()
    try:
        if body.url.startswith("https://"):
            origin = extract_https_origin(body.url)
            if not preparation_service().origin_is_approved(origin):
                raise ValueError("Destination origin has not been explicitly approved.")
            validate_public_https_url(body.url)
            mgr.approve_origin(origin)
        res = await mgr.navigate(body.url)
    except (ValueError, UnsafeDestination) as e:
        raise HTTPException(403, str(e))
    except Exception as e:
        raise HTTPException(500, f"Navigation failed: {e}")

    with db() as c:
        c.execute("UPDATE meta SET value=? WHERE key='current_origin'", (body.url,))
        event(c, "navigation", f"Automated browser navigated to {body.url}")

    return {**res, "state": state()}


@app.post("/api/automation/fill")
async def automation_fill() -> dict[str, Any]:
    """Fill approved form fields in Playwright Chromium browser and verify via action ledger."""
    mgr = get_playwright_manager()
    page = await mgr.get_page()

    with db() as c:
        current_origin = c.execute("SELECT value FROM meta WHERE key='current_origin'").fetchone()[0]
        if current_origin not in {p["url"] for p in PORTALS.values()}:
            raise HTTPException(403, "Current origin is outside approved scope")

        current = get_current_facts(c)
        proposed_receipts = c.execute(
            "SELECT * FROM receipts WHERE origin=? AND status='PROPOSED' AND permission='APPROVED'",
            (current_origin,),
        ).fetchall()

        field_values: dict[str, str] = {}
        for rec in proposed_receipts:
            fact = current.get(rec["semantic_type"])
            if not fact or fact["verification"] in {"UNVERIFIED", "UNVERIFIED_CONFLICT"} or not fact["user_confirmed"]:
                continue
            field_values[rec["destination_field"]] = fact["value"]

    # Fill fields via Playwright (safety guarantee: FormAutomator never clicks submission buttons)
    fill_outcome = await FormAutomator.fill_fields(page, field_values)
    observed = fill_outcome["observed"]

    # Reconcile observed values with the action ledger
    fill_state = fill(FillBody(origin=current_origin, observed=observed))

    # Capture updated screenshot
    try:
        await mgr.capture_screenshot()
    except Exception:
        pass

    return {
        "ok": True,
        "filled": fill_outcome["filled"],
        "observed": observed,
        "errors": fill_outcome["errors"],
        **fill_state,
    }


@app.post("/api/automation/recover")
async def automation_recover() -> dict[str, Any]:
    """Read live Playwright DOM and reconcile with action ledger."""
    mgr = get_playwright_manager()
    page = await mgr.get_page()

    with db() as c:
        current_origin = c.execute("SELECT value FROM meta WHERE key='current_origin'").fetchone()[0]
        if current_origin not in {p["url"] for p in PORTALS.values()}:
            raise HTTPException(403, "Current origin outside approved scope")

    observed = await FormAutomator.observe_fields(page)
    rec_state = recover(FillBody(origin=current_origin, observed=observed))

    try:
        await mgr.capture_screenshot()
    except Exception:
        pass

    return {
        "ok": True,
        "observed": observed,
        **rec_state,
    }


@app.get("/api/automation/screenshot")
async def automation_screenshot() -> Response:
    """Stream live screenshot of the Playwright browser."""
    mgr = get_playwright_manager()
    if not mgr.is_running:
        return Response(status_code=404, content="Browser is not running", media_type="text/plain")
    try:
        data = await mgr.capture_screenshot()
        return Response(content=data, media_type="image/png")
    except Exception as e:
        raise HTTPException(500, f"Screenshot capture failed: {e}")

