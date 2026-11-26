from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backend.app.discovery.schemas import Opportunity
from .schemas import ApplicationStatus


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS saved_opportunities (
        id TEXT PRIMARY KEY,
        identity_key TEXT NOT NULL UNIQUE,
        snapshot_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('SAVED','PREPARING','APPLIED','INTERVIEW','OFFER','REJECTED','WITHDRAWN','EXPIRED')),
        saved_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        applied_at TEXT,
        removed_at TEXT
    );
    CREATE TABLE IF NOT EXISTS opportunity_status_history (
        id TEXT PRIMARY KEY,
        opportunity_id TEXT NOT NULL REFERENCES saved_opportunities(id) ON DELETE CASCADE,
        previous_status TEXT,
        new_status TEXT NOT NULL,
        changed_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_opportunity_history ON opportunity_status_history(opportunity_id, changed_at DESC);
    CREATE TABLE IF NOT EXISTS opportunity_notes (
        id TEXT PRIMARY KEY,
        opportunity_id TEXT NOT NULL REFERENCES saved_opportunities(id) ON DELETE CASCADE,
        note TEXT NOT NULL CHECK(length(note) BETWEEN 1 AND 2000),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_opportunity_notes ON opportunity_notes(opportunity_id, created_at);
    CREATE TABLE IF NOT EXISTS chat_search_context (
        session_id TEXT NOT NULL,
        opportunity_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(session_id, opportunity_id),
        UNIQUE(session_id, ordinal)
    );
    CREATE INDEX IF NOT EXISTS idx_chat_context_recent ON chat_search_context(session_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS chat_search_sessions (
        session_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(saved_opportunities)")}
    if "removed_at" not in columns:
        connection.execute("ALTER TABLE saved_opportunities ADD COLUMN removed_at TEXT")


class OpportunityNotFound(LookupError):
    pass


class SearchReferenceError(ValueError):
    pass


class AmbiguousReference(SearchReferenceError):
    def __init__(self, choices: list[dict]):
        self.choices = choices
        super().__init__("More than one saved opportunity matches that reference.")


class OpportunityService:
    """Persistent local saved-opportunity, tracking, history, notes and search-context service."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]):
        self._connection_factory = connection_factory

    @staticmethod
    def _identity(item: Opportunity) -> str:
        parsed = urlparse(item.listing_url)
        query = urlencode([(key, value) for key, value in parse_qsl(parsed.query) if not key.casefold().startswith("utm_")])
        canonical = urlunparse(("https", (parsed.hostname or "").casefold(), parsed.path.rstrip("/"), "", query, ""))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        item = Opportunity.model_validate(json.loads(row["snapshot_json"]))
        notes = [dict(n) for n in connection.execute(
            "SELECT id,note,created_at,updated_at FROM opportunity_notes WHERE opportunity_id=? ORDER BY created_at DESC",
            (row["id"],),
        )]
        return {
            "id": row["id"], "opportunity": item.model_dump(mode="json"), "status": row["status"],
            "saved_at": row["saved_at"], "updated_at": row["updated_at"], "applied_at": row["applied_at"], "notes": notes,
        }

    def remember_search(self, session_id: str, opportunities: list[Opportunity], request: dict | None = None) -> None:
        timestamp = now_iso()
        with self._connection_factory() as connection:
            connection.execute("DELETE FROM chat_search_context WHERE session_id=?", (session_id,))
            if request is not None:
                connection.execute("INSERT INTO chat_search_sessions(session_id,request_json,created_at) VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET request_json=excluded.request_json,created_at=excluded.created_at",
                                   (session_id, json.dumps(request), timestamp))
            for ordinal, item in enumerate(opportunities[:20], 1):
                connection.execute(
                    "INSERT INTO chat_search_context(session_id,opportunity_id,ordinal,snapshot_json,created_at) VALUES(?,?,?,?,?)",
                    (session_id, item.id, ordinal, item.model_dump_json(), timestamp),
                )
            connection.execute("DELETE FROM chat_search_context WHERE replace(created_at,'T',' ') < datetime('now','-2 hours')")
            connection.execute("DELETE FROM chat_search_sessions WHERE replace(created_at,'T',' ') < datetime('now','-2 hours')")

    def latest_search_request(self, session_id: str) -> dict | None:
        with self._connection_factory() as connection:
            row = connection.execute("SELECT request_json FROM chat_search_sessions WHERE session_id=? AND replace(created_at,'T',' ') >= datetime('now','-2 hours')", (session_id,)).fetchone()
            return json.loads(row["request_json"]) if row else None

    def search_context(self, session_id: str) -> list[dict]:
        with self._connection_factory() as connection:
            rows = connection.execute(
                "SELECT opportunity_id,ordinal,snapshot_json FROM chat_search_context WHERE session_id=? AND replace(created_at,'T',' ') >= datetime('now','-2 hours') ORDER BY ordinal",
                (session_id,),
            ).fetchall()
            return [{"opportunity_id": r["opportunity_id"], "ordinal": r["ordinal"], "opportunity": json.loads(r["snapshot_json"])} for r in rows]

    def save_from_context(self, session_id: str, opportunity_id: str) -> tuple[dict, bool]:
        with self._connection_factory() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM chat_search_context WHERE session_id=? AND opportunity_id=?",
                (session_id, opportunity_id),
            ).fetchone()
            if not row:
                raise OpportunityNotFound("That listing is no longer in this chat’s recent search results. Search again to save it.")
            item = Opportunity.model_validate(json.loads(row["snapshot_json"]))
            identity = self._identity(item)
            existing = connection.execute("SELECT * FROM saved_opportunities WHERE identity_key=?", (identity,)).fetchone()
            if existing and existing["removed_at"] is None:
                return self._record(connection, existing), False
            timestamp = now_iso()
            if existing:
                connection.execute("UPDATE saved_opportunities SET snapshot_json=?,status='SAVED',updated_at=?,removed_at=NULL WHERE id=?",
                                   (item.model_dump_json(), timestamp, existing["id"]))
                connection.execute("INSERT INTO opportunity_status_history(id,opportunity_id,previous_status,new_status,changed_at) VALUES(?,?,?,?,?)",
                                   (str(uuid.uuid4()), existing["id"], existing["status"], "SAVED", timestamp))
                active = connection.execute("SELECT * FROM saved_opportunities WHERE id=?", (existing["id"],)).fetchone()
                return self._record(connection, active), True
            saved_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO saved_opportunities(id,identity_key,snapshot_json,status,saved_at,updated_at,applied_at) VALUES(?,?,?,?,?,?,NULL)",
                (saved_id, identity, item.model_dump_json(), "SAVED", timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO opportunity_status_history(id,opportunity_id,previous_status,new_status,changed_at) VALUES(?,?,?,?,?)",
                (str(uuid.uuid4()), saved_id, None, "SAVED", timestamp),
            )
            created = connection.execute("SELECT * FROM saved_opportunities WHERE id=?", (saved_id,)).fetchone()
            return self._record(connection, created), True

    def list_saved(self, status: ApplicationStatus | None = None, sort_by_deadline: bool = False) -> list[dict]:
        with self._connection_factory() as connection:
            if status:
                rows = connection.execute("SELECT * FROM saved_opportunities WHERE status=? AND removed_at IS NULL ORDER BY saved_at DESC", (status,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM saved_opportunities WHERE removed_at IS NULL ORDER BY saved_at DESC").fetchall()
            result = [self._record(connection, row) for row in rows]
            if sort_by_deadline:
                result.sort(key=lambda rec: (rec["opportunity"].get("application_deadline") is None,
                                             rec["opportunity"].get("application_deadline") or "9999-12-31", rec["saved_at"]))
            return result

    def get_saved(self, opportunity_id: str) -> dict:
        self._validate_id(opportunity_id)
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM saved_opportunities WHERE id=? AND removed_at IS NULL", (opportunity_id,)).fetchone()
            if not row:
                raise OpportunityNotFound("Saved opportunity not found.")
            return self._record(connection, row)

    @staticmethod
    def _validate_id(value: str) -> None:
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Invalid saved opportunity ID.") from exc

    def remove(self, opportunity_id: str) -> bool:
        self._validate_id(opportunity_id)
        with self._connection_factory() as connection:
            timestamp = now_iso()
            cursor = connection.execute("UPDATE saved_opportunities SET removed_at=?,updated_at=? WHERE id=? AND removed_at IS NULL", (timestamp, timestamp, opportunity_id))
            if cursor.rowcount:
                connection.execute("DELETE FROM opportunity_notes WHERE opportunity_id=?", (opportunity_id,))
            return cursor.rowcount > 0

    def update_status(self, opportunity_id: str, status: ApplicationStatus) -> dict:
        self._validate_id(opportunity_id)
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM saved_opportunities WHERE id=? AND removed_at IS NULL", (opportunity_id,)).fetchone()
            if not row:
                raise OpportunityNotFound("Saved opportunity not found.")
            if row["status"] == status:
                return self._record(connection, row)
            timestamp = now_iso()
            applied_at = row["applied_at"] or (timestamp if status == "APPLIED" else None)
            connection.execute("UPDATE saved_opportunities SET status=?,updated_at=?,applied_at=? WHERE id=?",
                               (status, timestamp, applied_at, opportunity_id))
            connection.execute("INSERT INTO opportunity_status_history(id,opportunity_id,previous_status,new_status,changed_at) VALUES(?,?,?,?,?)",
                               (str(uuid.uuid4()), opportunity_id, row["status"], status, timestamp))
            updated = connection.execute("SELECT * FROM saved_opportunities WHERE id=?", (opportunity_id,)).fetchone()
            return self._record(connection, updated)

    def history(self, opportunity_id: str | None = None, company: str | None = None) -> list[dict]:
        if opportunity_id is not None:
            self._validate_id(opportunity_id)
        with self._connection_factory() as connection:
            if opportunity_id:
                exists = connection.execute("SELECT 1 FROM saved_opportunities WHERE id=?", (opportunity_id,)).fetchone()
                if not exists:
                    raise OpportunityNotFound("Saved opportunity not found.")
                rows = connection.execute("SELECT h.*,s.snapshot_json FROM opportunity_status_history h JOIN saved_opportunities s ON s.id=h.opportunity_id WHERE h.opportunity_id=? ORDER BY h.changed_at DESC,h.rowid DESC", (opportunity_id,))
            else:
                rows = connection.execute("SELECT h.*,s.snapshot_json FROM opportunity_status_history h JOIN saved_opportunities s ON s.id=h.opportunity_id ORDER BY h.changed_at DESC,h.rowid DESC")
            records = [dict(row) for row in rows]
            for record in records:
                if "snapshot_json" in record:
                    snap = json.loads(record.pop("snapshot_json"))
                    record["title"] = snap.get("title")
                    record["company_name"] = snap.get("company_name")
            if company:
                needle = company.casefold().strip()
                records = [record for record in records if needle in " ".join((record.get("company_name") or "", record.get("title") or "")).casefold()]
            return records

    def add_note(self, opportunity_id: str, note: str) -> dict:
        self._validate_id(opportunity_id)
        note = note.strip()
        if not note or len(note) > 2000:
            raise ValueError("Notes must contain 1 to 2000 characters.")
        timestamp, note_id = now_iso(), str(uuid.uuid4())
        with self._connection_factory() as connection:
            if not connection.execute("SELECT 1 FROM saved_opportunities WHERE id=?", (opportunity_id,)).fetchone():
                raise OpportunityNotFound("Saved opportunity not found.")
            connection.execute("INSERT INTO opportunity_notes VALUES(?,?,?,?,?)", (note_id, opportunity_id, note, timestamp, timestamp))
            return dict(connection.execute("SELECT id,note,created_at,updated_at FROM opportunity_notes WHERE id=?", (note_id,)).fetchone())

    def update_note(self, opportunity_id: str, note_id: str, note: str) -> dict:
        self._validate_id(opportunity_id)
        self._validate_id(note_id)
        timestamp = now_iso()
        with self._connection_factory() as connection:
            cursor = connection.execute("UPDATE opportunity_notes SET note=?,updated_at=? WHERE id=? AND opportunity_id=?", (note, timestamp, note_id, opportunity_id))
            if not cursor.rowcount:
                raise OpportunityNotFound("Note not found for this opportunity.")
            return dict(connection.execute("SELECT id,note,created_at,updated_at FROM opportunity_notes WHERE id=?", (note_id,)).fetchone())

    def delete_note(self, opportunity_id: str, note_id: str) -> bool:
        self._validate_id(opportunity_id)
        self._validate_id(note_id)
        with self._connection_factory() as connection:
            cursor = connection.execute("DELETE FROM opportunity_notes WHERE id=? AND opportunity_id=?", (note_id, opportunity_id))
            return cursor.rowcount > 0

    def resolve_context(self, session_id: str, ordinal: int | None = None, company: str | None = None) -> list[dict]:
        rows = self.search_context(session_id)
        return self._resolve(rows, ordinal, company, lambda row: row["opportunity"])

    def resolve_saved(self, ordinal: int | None = None, company: str | None = None) -> list[dict]:
        rows = self.list_saved()
        return self._resolve(rows, ordinal, company, lambda row: row["opportunity"])

    @staticmethod
    def _resolve(rows: list[dict], ordinal: int | None, company: str | None, unwrap: Callable[[dict], dict]) -> list[dict]:
        if ordinal is not None:
            if ordinal < 1 or ordinal > len(rows):
                return []
            return [rows[ordinal - 1]]
        if company:
            needle = re.sub(r"\s+", " ", company).casefold().strip()
            matches = [row for row in rows if needle in " ".join(str(unwrap(row).get(k) or "") for k in ("company_name", "title")).casefold()]
            if len(matches) > 1:
                raise AmbiguousReference([{ "id": row.get("id") or row.get("opportunity_id"), "title": unwrap(row).get("title"), "company_name": unwrap(row).get("company_name") } for row in matches])
            return matches
        return []
