from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any


STATUSES = {"CREATED", "PLANNING", "RUNNING", "WAITING_FOR_USER", "WAITING_FOR_APPROVAL", "PAUSED", "PARTIALLY_COMPLETED", "COMPLETED", "FAILED", "CANCELLED"}
TRANSITIONS = {
    "CREATED": {"PLANNING", "RUNNING", "WAITING_FOR_USER", "CANCELLED", "FAILED"},
    "PLANNING": {"RUNNING", "WAITING_FOR_USER", "WAITING_FOR_APPROVAL", "PAUSED", "FAILED", "CANCELLED"},
    "RUNNING": {"WAITING_FOR_USER", "WAITING_FOR_APPROVAL", "PAUSED", "PARTIALLY_COMPLETED", "COMPLETED", "FAILED", "CANCELLED"},
    "WAITING_FOR_USER": {"PLANNING", "RUNNING", "PAUSED", "COMPLETED", "CANCELLED", "FAILED"},
    "WAITING_FOR_APPROVAL": {"RUNNING", "PAUSED", "CANCELLED", "FAILED"},
    "PAUSED": {"PLANNING", "RUNNING", "WAITING_FOR_USER", "CANCELLED"},
    "PARTIALLY_COMPLETED": {"PLANNING", "RUNNING", "WAITING_FOR_USER", "WAITING_FOR_APPROVAL", "PAUSED", "COMPLETED", "CANCELLED", "FAILED"},
    "COMPLETED": set(), "FAILED": {"PLANNING", "RUNNING", "CANCELLED"}, "CANCELLED": set(),
}


class WorkflowError(ValueError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"\b(password|passcode|otp|one[- ]time\s+code|mfa\s+code|auth(?:orization)?\s+token|access\s+token|refresh\s+token|api[-_ ]?key|secret|cookie)\b\s*(?:is\s+|[:=]\s*|\s+)[\"']?([^\s,;\"']+)", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


def safe_persisted_text(value: str, limit: int) -> str:
    """Redact explicit credential-like values before retaining user text in workflow state."""
    safe = str(value)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            safe = pattern.sub(lambda match: f"{match.group(1)} [redacted]", safe)
        else:
            safe = pattern.sub("[redacted credential]", safe)
    return safe[:limit]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_schema(c: sqlite3.Connection) -> None:
    c.executescript("""
    CREATE TABLE IF NOT EXISTS agent_workflows(
      id TEXT PRIMARY KEY, session_id TEXT NOT NULL, goal TEXT NOT NULL, goal_type TEXT NOT NULL,
      status TEXT NOT NULL, plan TEXT NOT NULL, memory TEXT NOT NULL, current_step TEXT,
      pending_action TEXT, approval_requirements TEXT NOT NULL DEFAULT '[]', recovery_state TEXT,
      error TEXT, completion_summary TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      last_activity TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_agent_workflows_session ON agent_workflows(session_id, updated_at DESC);
    CREATE TABLE IF NOT EXISTS workflow_activity(
      id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES agent_workflows(id) ON DELETE CASCADE,
      kind TEXT NOT NULL, summary TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS workflow_checkpoints(
      id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES agent_workflows(id) ON DELETE CASCADE,
      step TEXT NOT NULL, status TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
      UNIQUE(workflow_id, step, status)
    );
    CREATE TABLE IF NOT EXISTS workflow_decisions(
      id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES agent_workflows(id) ON DELETE CASCADE,
      prompt TEXT NOT NULL, status TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    """)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("plan", "memory", "approval_requirements"):
        item[key] = json.loads(item[key])
    return item


class WorkflowService:
    def __init__(self, connect):
        self.connect = connect

    def _activity(self, c, workflow_id: str, kind: str, summary: str, data: dict | None = None):
        stamp = now()
        c.execute("INSERT INTO workflow_activity VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), workflow_id, kind, summary[:500], json.dumps(data or {}, separators=(",", ":")), stamp))
        c.execute("UPDATE agent_workflows SET updated_at=?,last_activity=? WHERE id=?", (stamp, stamp, workflow_id))

    def create(self, goal: str, session_id: str = "default") -> dict:
        goal = goal.strip()
        session_id = session_id.strip()
        if not session_id or len(session_id) > 64 or any(ord(character) < 32 for character in session_id):
            raise WorkflowError("Session ID is invalid.")
        if len(goal) < 5 or len(goal) > 1000:
            raise WorkflowError("Goal must contain 5 to 1000 characters.")
        goal = safe_persisted_text(goal, 1000)
        goal_type = "internship_discovery" if any(w in goal.casefold() for w in ("intern", "job", "opportunit")) else "career_workflow"
        plan = [{"step": "understand_goal", "status": "completed"}, {"step": "use_existing_tools", "status": "pending"}, {"step": "review_results_with_user", "status": "pending"}]
        stamp, wid = now(), str(uuid.uuid4())
        with self.connect() as c:
            active = c.execute("SELECT id FROM agent_workflows WHERE session_id=? AND status NOT IN ('COMPLETED','CANCELLED','FAILED') ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
            if active:
                return self.get(active["id"])
            c.execute("INSERT INTO agent_workflows(id,session_id,goal,goal_type,status,plan,memory,current_step,pending_action,created_at,updated_at,last_activity) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      (wid, session_id, goal, goal_type, "RUNNING", json.dumps(plan), json.dumps({"selected_opportunity_ids": [], "preparation_ids": [], "completed_steps": [], "failed_steps": [], "receipt_ids": [], "search_criteria": {}}), "use_existing_tools", None, stamp, stamp, stamp))
            self._activity(c, wid, "goal_created", "Goal captured and bounded plan created.", {"goal_type": goal_type})
            self._checkpoint(c, wid, "goal_created", "completed", {})
        return self.get(wid)

    def _checkpoint(self, c, wid, step, status, refs):
        c.execute("INSERT OR IGNORE INTO workflow_checkpoints VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), wid, step[:100], status[:40], json.dumps(refs or {}, separators=(",", ":")), now()))

    def record_tool(self, session_id: str, tool_name: str, result: Any, error: str | None = None):
        with self.connect() as c:
            row = c.execute("SELECT * FROM agent_workflows WHERE session_id=? AND status NOT IN ('COMPLETED','CANCELLED') ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
            if not row:
                return
            wid = row["id"]
            if error:
                self._activity(c, wid, "step_failed", f"{tool_name} needs attention.", {})
                c.execute("UPDATE agent_workflows SET status='PARTIALLY_COMPLETED',error=?,recovery_state='Inspect existing records before retrying.' WHERE id=?", (error[:300], wid))
                self._checkpoint(c, wid, tool_name, "failed", {})
                return
            data = getattr(result, "data", {}) or {}
            refs: dict[str, Any] = {}
            if tool_name == "search_internships":
                refs["result_count"] = len(data.get("opportunities", []))
                refs["search_criteria"] = data.get("query_summary", {})
            elif tool_name == "save_opportunity":
                ids = [x.get("id") for x in data.get("saved", []) if x.get("id")]
                mem = json.loads(row["memory"]); mem["selected_opportunity_ids"] = list(dict.fromkeys(mem.get("selected_opportunity_ids", []) + ids))
                c.execute("UPDATE agent_workflows SET memory=? WHERE id=?", (json.dumps(mem), wid)); refs["opportunity_ids"] = ids
            elif tool_name == "get_application_preparation":
                prep = data.get("preparation") or {}; refs["preparation_id"] = prep.get("id")
                mem = json.loads(row["memory"])
                if prep.get("id"): mem["preparation_ids"] = list(dict.fromkeys(mem.get("preparation_ids", []) + [prep["id"]]))
                c.execute("UPDATE agent_workflows SET memory=? WHERE id=?", (json.dumps(mem), wid))
            safe_summary = f"{tool_name.replace('_', ' ').capitalize()} completed."
            if tool_name == "search_internships": safe_summary = f"Search completed with {refs.get('result_count', 0)} listing(s)."
            if tool_name == "save_opportunity": safe_summary = f"Saved {len(refs.get('opportunity_ids', []))} selected opportunity record(s)."
            self._activity(c, wid, "step_completed", safe_summary, {k:v for k,v in refs.items() if k != "search_criteria"})
            # Chat executes one validated tool per user message; continuation always waits for a user instruction.
            status = "WAITING_FOR_USER"
            if tool_name == "search_internships":
                mem = json.loads(row["memory"]); mem["search_criteria"] = refs.get("search_criteria") or {}
                c.execute("UPDATE agent_workflows SET memory=? WHERE id=?", (json.dumps(mem), wid))
            c.execute("UPDATE agent_workflows SET status=?,current_step=?,pending_action=?,error=NULL,recovery_state=NULL WHERE id=?", (status, tool_name, "Tell CarryFlow what you’d like to do next.", wid))
            self._checkpoint(c, wid, tool_name, "completed", refs)

    def get(self, workflow_id: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT * FROM agent_workflows WHERE id=?", (workflow_id,)).fetchone()
            if not row: raise WorkflowError("Workflow not found.")
            item = _decode(row)
            item["activity"] = [dict(x, data=json.loads(x["data"])) for x in c.execute("SELECT * FROM workflow_activity WHERE workflow_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (workflow_id,))]
            item["checkpoints"] = [dict(x, refs=json.loads(x["refs"])) for x in c.execute("SELECT * FROM workflow_checkpoints WHERE workflow_id=? ORDER BY created_at,rowid", (workflow_id,))]
            item["decisions"] = [dict(x) for x in c.execute("SELECT * FROM workflow_decisions WHERE workflow_id=? AND status='pending' ORDER BY created_at,rowid", (workflow_id,))]
            return item

    def get_owned(self, workflow_id: str, session_id: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT 1 FROM agent_workflows WHERE id=? AND session_id=?", (workflow_id, session_id)).fetchone()
        if not row:
            raise WorkflowError("Workflow not found.")
        return self.get(workflow_id)

    def list(self, session_id: str | None = None) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT id FROM agent_workflows WHERE (? IS NULL OR session_id=?) ORDER BY updated_at DESC LIMIT 100", (session_id, session_id)).fetchall()
        return [self.get(row["id"]) for row in rows]

    def active(self, session_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT id FROM agent_workflows WHERE session_id=? AND status NOT IN ('COMPLETED','CANCELLED','FAILED') ORDER BY updated_at DESC LIMIT 1", (session_id,)).fetchone()
        return self.get(row["id"]) if row else None

    def transition(self, workflow_id: str, action: str, response: str | None = None) -> dict:
        targets = {"pause": "PAUSED", "resume": "RUNNING", "cancel": "CANCELLED", "complete": "COMPLETED"}
        if action not in targets: raise WorkflowError("Unknown workflow action.")
        target = targets[action]
        with self.connect() as c:
            row = c.execute("SELECT * FROM agent_workflows WHERE id=?", (workflow_id,)).fetchone()
            if not row: raise WorkflowError("Workflow not found.")
            if target not in TRANSITIONS[row["status"]]: raise WorkflowError(f"Cannot {action} a {row['status'].lower()} workflow.")
            stamp = now()
            if action in {"cancel", "complete", "resume"}:
                c.execute("UPDATE workflow_decisions SET status='cancelled',updated_at=? WHERE workflow_id=? AND status='pending'", (stamp, workflow_id))
            c.execute("UPDATE agent_workflows SET status=?,pending_action=?,recovery_state=?,completion_summary=CASE WHEN ?='complete' THEN 'Marked complete by the user.' ELSE completion_summary END,updated_at=?,last_activity=? WHERE id=?", (target, "Continue when you are ready." if action == "pause" else None, "Recovery will inspect saved records before retrying." if action == "resume" else None, action, stamp, stamp, workflow_id))
            self._activity(c, workflow_id, action, {"pause":"Workflow paused.","resume":"Workflow resumed from its last checkpoint.","cancel":"Workflow cancelled.","complete":"Workflow marked complete by the user."}[action], {})
        return self.get(workflow_id)

    def ask(self, workflow_id: str, prompt: str) -> dict:
        with self.connect() as c:
            row = c.execute("SELECT status FROM agent_workflows WHERE id=?", (workflow_id,)).fetchone()
            if not row: raise WorkflowError("Workflow not found.")
            existing = c.execute("SELECT id FROM workflow_decisions WHERE workflow_id=? AND status='pending' AND prompt=? ORDER BY created_at DESC LIMIT 1", (workflow_id, prompt[:500])).fetchone()
            if existing: return self.get(workflow_id)
            if "WAITING_FOR_USER" not in TRANSITIONS[row["status"]]: raise WorkflowError("This workflow cannot accept a decision right now.")
            stamp, did = now(), str(uuid.uuid4())
            safe_prompt = safe_persisted_text(prompt, 500)
            c.execute("INSERT INTO workflow_decisions VALUES(?,?,?,?,?,?,?)", (did, workflow_id, safe_prompt, "pending", None, stamp, stamp))
            c.execute("UPDATE agent_workflows SET status='WAITING_FOR_USER',pending_action=?,updated_at=?,last_activity=? WHERE id=?", (safe_prompt, stamp, stamp, workflow_id))
            self._activity(c, workflow_id, "decision_requested", safe_prompt, {})
        return self.get(workflow_id)

    def respond(self, workflow_id: str, decision_id: str, response: str) -> dict:
        with self.connect() as c:
            workflow = c.execute("SELECT status FROM agent_workflows WHERE id=?", (workflow_id,)).fetchone()
            if not workflow:
                raise WorkflowError("Workflow not found.")
            if workflow["status"] != "WAITING_FOR_USER":
                raise WorkflowError("This workflow is no longer waiting for that decision.")
            row = c.execute("SELECT * FROM workflow_decisions WHERE id=? AND workflow_id=? AND status='pending'", (decision_id, workflow_id)).fetchone()
            if not row: raise WorkflowError("Pending decision not found.")
            stamp = now()
            safe_response = safe_persisted_text(response, 500)
            c.execute("UPDATE workflow_decisions SET status='answered',response=?,updated_at=? WHERE id=?", (safe_response, stamp, decision_id))
            mem = json.loads(c.execute("SELECT memory FROM agent_workflows WHERE id=?", (workflow_id,)).fetchone()[0]); mem["pending_decision"] = safe_response
            c.execute("UPDATE agent_workflows SET memory=?,status='RUNNING',pending_action=NULL,updated_at=?,last_activity=? WHERE id=?", (json.dumps(mem), stamp, stamp, workflow_id))
            self._activity(c, workflow_id, "decision_received", "Your decision was saved.", {})
        return self.get(workflow_id)
