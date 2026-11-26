from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import HTTPException

from backend.app.passport import FactCreateBody, add_or_version_fact, confirm_fact_version
from .extractor import ResumeInputError, parse_resume, validate_and_extract
from .schemas import ResumeDetail, ResumeItem, ResumeItemUpdate, ResumeRecord


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS resumes (
        id TEXT PRIMARY KEY,
        filename TEXT NOT NULL,
        file_type TEXT NOT NULL CHECK(file_type IN ('PDF','DOCX')),
        size_bytes INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        uploaded_at TEXT NOT NULL,
        extraction_status TEXT NOT NULL DEFAULT 'READY',
        warning TEXT
    );
    CREATE TABLE IF NOT EXISTS resume_items (
        id TEXT NOT NULL,
        resume_id TEXT NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
        payload_json TEXT NOT NULL,
        review_status TEXT NOT NULL CHECK(review_status IN ('EXTRACTED','EDITED','USER_VERIFIED','REJECTED','CONFLICT_PENDING')),
        passport_fact_id TEXT,
        passport_version INTEGER,
        review_error TEXT,
        PRIMARY KEY(resume_id,id)
    );
    CREATE INDEX IF NOT EXISTS idx_resume_items_review ON resume_items(resume_id,review_status);
    """)


class ResumeService:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]):
        self._connection_factory = connection_factory

    @staticmethod
    def _validate_id(value: str) -> None:
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise HTTPException(422, "Invalid resume identifier") from exc

    @staticmethod
    def _record(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        counts = connection.execute(
            "SELECT count(*) AS total, sum(CASE WHEN review_status='USER_VERIFIED' THEN 1 ELSE 0 END) AS verified FROM resume_items WHERE resume_id=?",
            (row["id"],),
        ).fetchone()
        return ResumeRecord(
            id=row["id"], filename=row["filename"], file_type=row["file_type"], size_bytes=row["size_bytes"],
            uploaded_at=row["uploaded_at"], extraction_status=row["extraction_status"],
            item_count=counts["total"] or 0, verified_count=counts["verified"] or 0, warning=row["warning"],
        ).model_dump(mode="json")

    @staticmethod
    def _item(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        payload = json.loads(row["payload_json"])
        payload.update(review_status=row["review_status"], passport_fact_id=row["passport_fact_id"],
                       passport_version=row["passport_version"], review_error=row["review_error"])
        return ResumeItem.model_validate(payload).model_dump(mode="json")

    def upload(self, filename: str | None, content: bytes) -> dict:
        safe_name, extracted = validate_and_extract(filename, content)
        items = parse_resume(extracted.text)
        if not items:
            raise ResumeInputError("We could read the document, but couldn’t identify any resume details to review.")
        resume_id = str(uuid.uuid4())
        uploaded_at = _now()
        warning = "Scanned PDF detected; OCR is not available." if extracted.file_type == "PDF" and len(extracted.text.strip()) < 100 else None
        # The original file is deliberately not retained. Only normalized, reviewable
        # candidate facts and a document hash are stored in the private local database.
        with self._connection_factory() as connection:
            connection.execute(
                "INSERT INTO resumes(id,filename,file_type,size_bytes,sha256,uploaded_at,extraction_status,warning) VALUES(?,?,?,?,?,?,?,?)",
                (resume_id, safe_name, extracted.file_type, len(content), hashlib.sha256(content).hexdigest(), uploaded_at, "READY", warning),
            )
            for item in items:
                payload = item.model_dump(mode="json")
                connection.execute(
                    "INSERT INTO resume_items(id,resume_id,payload_json,review_status,passport_fact_id,passport_version,review_error) VALUES(?,?,?,?,?,?,NULL)",
                    (item.id, resume_id, json.dumps(payload, ensure_ascii=False), "EXTRACTED", None, None),
                )
            resume = connection.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
            rows = connection.execute("SELECT * FROM resume_items WHERE resume_id=? ORDER BY rowid", (resume_id,)).fetchall()
            return {"resume": self._record(connection, resume), "items": [self._item(connection, row) for row in rows], "warnings": list(extracted.warnings)}

    def list_resumes(self) -> list[dict]:
        with self._connection_factory() as connection:
            rows = connection.execute("SELECT * FROM resumes ORDER BY uploaded_at DESC").fetchall()
            return [self._record(connection, row) for row in rows]

    def get_resume(self, resume_id: str) -> dict:
        self._validate_id(resume_id)
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Resume not found")
            items = connection.execute("SELECT * FROM resume_items WHERE resume_id=? ORDER BY rowid", (resume_id,)).fetchall()
            return ResumeDetail(resume=self._record(connection, row), items=[self._item(connection, item) for item in items]).model_dump(mode="json")

    def update_item(self, resume_id: str, item_id: str, body: ResumeItemUpdate) -> dict:
        self._validate_id(resume_id)
        self._validate_id(item_id)
        with self._connection_factory() as connection:
            row = connection.execute("SELECT * FROM resume_items WHERE resume_id=? AND id=?", (resume_id, item_id)).fetchone()
            if not row:
                raise HTTPException(404, "Extracted resume item not found")
            if row["review_status"] not in {"EXTRACTED", "EDITED"}:
                raise HTTPException(409, "Only unreviewed extracted information can be edited")
            payload = json.loads(row["payload_json"])
            if payload.get("original_value") is None:
                payload["original_value"] = payload["value"]
            payload["value"] = body.value
            if body.details is not None:
                payload["details"] = body.details
            connection.execute("UPDATE resume_items SET payload_json=?,review_status='EDITED',review_error=NULL WHERE resume_id=? AND id=?",
                               (json.dumps(payload, ensure_ascii=False), resume_id, item_id))
            updated = connection.execute("SELECT * FROM resume_items WHERE resume_id=? AND id=?", (resume_id, item_id)).fetchone()
            return self._item(connection, updated)

    def reject_items(self, resume_id: str, item_ids: list[str]) -> dict:
        self._validate_id(resume_id)
        if len(set(item_ids)) != len(item_ids):
            raise HTTPException(422, "Repeated resume item identifiers are not allowed")
        with self._connection_factory() as connection:
            results = []
            for item_id in item_ids:
                self._validate_id(item_id)
                row = connection.execute("SELECT * FROM resume_items WHERE resume_id=? AND id=?", (resume_id, item_id)).fetchone()
                if not row:
                    raise HTTPException(404, "Extracted resume item not found")
                if row["review_status"] in {"EXTRACTED", "EDITED"}:
                    connection.execute("UPDATE resume_items SET review_status='REJECTED',review_error=NULL WHERE resume_id=? AND id=?", (resume_id, item_id))
                results.append(self._item(connection, connection.execute("SELECT * FROM resume_items WHERE resume_id=? AND id=?", (resume_id, item_id)).fetchone()))
            return {"items": results, "errors": {}}

    def approve_items(self, resume_id: str, item_ids: list[str]) -> dict:
        self._validate_id(resume_id)
        if len(set(item_ids)) != len(item_ids):
            raise HTTPException(422, "Repeated resume item identifiers are not allowed")
        with self._connection_factory() as connection:
            resume = connection.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone()
            if not resume:
                raise HTTPException(404, "Resume not found")
            results: list[dict] = []
            errors: dict[str, str] = {}
            for item_id in item_ids:
                self._validate_id(item_id)
                row = connection.execute("SELECT * FROM resume_items WHERE resume_id=? AND id=?", (resume_id, item_id)).fetchone()
                if not row:
                    raise HTTPException(404, "Extracted resume item not found")
                if row["review_status"] not in {"EXTRACTED", "EDITED"}:
                    results.append(self._item(connection, row))
                    continue
                item = ResumeItem.model_validate(json.loads(row["payload_json"]))
                try:
                    value = self._passport_value(item)
                    matching = connection.execute(
                        "SELECT * FROM facts WHERE semantic_type=? AND value=? ORDER BY version DESC",
                        (item.semantic_type, value),
                    ).fetchall()
                    if matching:
                        exact = matching[0]
                        if not exact["user_confirmed"]:
                            confirm_fact_version(connection, exact["id"], exact["version"])
                        state, fact_id, version = "USER_VERIFIED", exact["id"], exact["version"]
                    else:
                        same_id = connection.execute("SELECT * FROM facts WHERE id=? ORDER BY version DESC", (item.target_fact_id,)).fetchall()
                        conflict = bool(same_id)
                        created = add_or_version_fact(connection, FactCreateBody(
                            id=item.target_fact_id,
                            label=item.label,
                            value=value,
                            semantic_type=item.semantic_type,
                            source_url=f"resume://{resume_id}",
                            source_label=f"{resume['filename']} · {item.section}"[:128],
                            permitted_origins=[],
                            user_confirmed=not conflict,
                            is_conflict=conflict,
                            source_type="resume_edited" if item.original_value is not None else "resume",
                            source_document_id=resume_id,
                            source_section=item.section,
                            source_evidence=item.evidence,
                            extraction_confidence=item.confidence,
                        ))
                        fact_id, version = created["id"], created["version"]
                        state = "CONFLICT_PENDING" if conflict else "USER_VERIFIED"
                    connection.execute("UPDATE resume_items SET review_status=?,passport_fact_id=?,passport_version=?,review_error=NULL WHERE resume_id=? AND id=?",
                                       (state, fact_id, version, resume_id, item_id))
                except (HTTPException, ValueError) as exc:
                    detail = getattr(exc, "detail", str(exc))
                    errors[item_id] = str(detail)[:300]
                    connection.execute("UPDATE resume_items SET review_error=? WHERE resume_id=? AND id=?", (errors[item_id], resume_id, item_id))
                updated = connection.execute("SELECT * FROM resume_items WHERE resume_id=? AND id=?", (resume_id, item_id)).fetchone()
                results.append(self._item(connection, updated))
            return {"items": results, "errors": errors}

    @staticmethod
    def _passport_value(item: ResumeItem) -> str:
        if item.category in {"project", "experience", "certification", "achievement", "activity", "research", "publication", "volunteering", "coursework"}:
            value = {"summary": item.value, "details": item.details, "section": item.section, "source_evidence": item.evidence}
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return item.value.strip()

    def delete_resume(self, resume_id: str) -> bool:
        self._validate_id(resume_id)
        with self._connection_factory() as connection:
            cursor = connection.execute("DELETE FROM resumes WHERE id=?", (resume_id,))
            return cursor.rowcount > 0

    def compare_with_passport(self, resume_id: str) -> dict:
        detail = self.get_resume(resume_id)
        comparisons = []
        with self._connection_factory() as connection:
            for item_data in detail["items"]:
                item = ResumeItem.model_validate(item_data)
                versions = connection.execute("SELECT * FROM facts WHERE id=? ORDER BY version DESC", (item.target_fact_id,)).fetchall()
                if not versions:
                    comparison = "not_in_passport"
                    passport_value = None
                    passport_version = None
                else:
                    unresolved = connection.execute("SELECT old_version,new_version FROM conflicts WHERE fact_id=? AND resolved=0 ORDER BY rowid DESC LIMIT 1", (item.target_fact_id,)).fetchone()
                    resolution = connection.execute("SELECT resolution_version FROM conflicts WHERE fact_id=? AND resolved=1 ORDER BY rowid DESC LIMIT 1", (item.target_fact_id,)).fetchone()
                    active = next((fact for fact in versions if resolution and fact["version"] == resolution["resolution_version"]), None)
                    if unresolved:
                        active = next((fact for fact in versions if fact["version"] == unresolved["old_version"]), active)
                    if not active:
                        active = next((fact for fact in versions if fact["verification"] != "SUPERSEDED"), versions[0])
                    passport_value = active["value"]
                    passport_version = active["version"]
                    comparison = "conflict_pending" if unresolved and item.passport_version == unresolved["new_version"] else "matches" if passport_value == self._passport_value(item) else "different"
                comparisons.append({"item": item_data, "comparison": comparison, "passport_value": passport_value, "passport_version": passport_version})
        return {"resume": detail["resume"], "comparisons": comparisons}

    def mark_conflict_resolution(self, fact_id: str, version: int) -> None:
        with self._connection_factory() as connection:
            pending = connection.execute(
                "SELECT * FROM resume_items WHERE passport_fact_id=? AND review_status='CONFLICT_PENDING'",
                (fact_id,),
            ).fetchall()
            for row in pending:
                state = "USER_VERIFIED" if row["passport_version"] == version else "REJECTED"
                connection.execute("UPDATE resume_items SET review_status=?,review_error=NULL WHERE resume_id=? AND id=?",
                                   (state, row["resume_id"], row["id"]))
