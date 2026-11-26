from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Callable

from backend.app.preparation.schemas import FormField
from backend.app.translator.canonical import CANONICAL_FIELDS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS approved_application_origins(
      origin TEXT PRIMARY KEY, approved_at TEXT NOT NULL, approved_by TEXT NOT NULL DEFAULT 'user');
    CREATE TABLE IF NOT EXISTS application_preparations(
      id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, title TEXT NOT NULL, company_name TEXT,
      destination_url TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
      fields_json TEXT NOT NULL DEFAULT '[]', receipt_json TEXT,
      approval_reference TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_preparations_created ON application_preparations(created_at DESC);
    CREATE TABLE IF NOT EXISTS application_preparation_events(
      id TEXT PRIMARY KEY, preparation_id TEXT NOT NULL REFERENCES application_preparations(id) ON DELETE CASCADE,
      event_type TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
    """)


class PreparationError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class PreparationService:
    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]):
        self._db = connection_factory

    def approved_origins(self) -> list[dict]:
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM approved_application_origins ORDER BY approved_at DESC")]

    def approve_origin(self, origin: str) -> dict:
        with self._db() as c:
            c.execute("INSERT OR IGNORE INTO approved_application_origins(origin,approved_at,approved_by) VALUES(?,?,'user')", (origin, _now()))
            row = c.execute("SELECT * FROM approved_application_origins WHERE origin=?", (origin,)).fetchone()
            return dict(row)

    def origin_is_approved(self, origin: str) -> bool:
        with self._db() as c:
            return bool(c.execute("SELECT 1 FROM approved_application_origins WHERE origin=?", (origin,)).fetchone())

    @staticmethod
    def _event(c, preparation_id: str, event_type: str, detail: str) -> None:
        c.execute("INSERT INTO application_preparation_events VALUES(?,?,?,?,?)", (str(uuid.uuid4()), preparation_id, event_type, detail[:500], _now()))

    @staticmethod
    def _transformed_value(value: str, transform: str | None) -> str:
        if not transform:
            return value
        parts = value.split()
        if len(parts) < 2:
            raise PreparationError("The verified full name cannot be safely split into this field. Review the name in Passport or leave the field blank.", 422)
        return parts[0] if transform == "first_name" else " ".join(parts[1:])

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "opportunity_id": row["opportunity_id"], "company_name": row["company_name"],
            "title": row["title"], "destination_url": row["destination_url"], "origin": row["origin"],
            "status": row["status"], "fields": json.loads(row["fields_json"]),
            "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
            "approval_reference": row["approval_reference"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def create(self, opportunity_id: str, opportunity: dict, destination_url: str, origin: str) -> dict:
        if not self.origin_is_approved(origin):
            raise PreparationError("This destination origin is not approved yet. Review and approve the origin before opening it.", 403)
        preparation_id, timestamp = str(uuid.uuid4()), _now()
        with self._db() as c:
            c.execute("INSERT INTO application_preparations(id,opportunity_id,title,company_name,destination_url,origin,status,fields_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'OPENING','[]',?,?)",
                      (preparation_id, opportunity_id, opportunity["title"], opportunity.get("company_name"), destination_url, origin, timestamp, timestamp))
            self._event(c, preparation_id, "browser_opening", "Saved opportunity destination opened after origin approval.")
            row = c.execute("SELECT * FROM application_preparations WHERE id=?", (preparation_id,)).fetchone()
        return self._record(row)

    def get(self, preparation_id: str) -> dict:
        with self._db() as c:
            row = c.execute("SELECT * FROM application_preparations WHERE id=?", (preparation_id,)).fetchone()
            if not row:
                raise PreparationError("Preparation not found.", 404)
            return self._record(row)

    def list(self) -> list[dict]:
        with self._db() as c:
            return [self._record(r) for r in c.execute("SELECT * FROM application_preparations ORDER BY created_at DESC LIMIT 30")]

    def set_inspection(self, preparation_id: str, inspected: list[dict], current_facts: list[dict], translator, origin: str, *, recovery: bool = False, excluded_count: int = 0) -> dict:
        fields = []
        fact_by_semantic = {f["semantic_type"]: f for f in current_facts}
        permitted_status = {"USER_CONFIRMED"}
        for raw in inspected[:200]:
            f = dict(raw)
            label = str(f.get("accessible_name") or f.get("label") or f.get("name") or f.get("element_id") or "Unlabeled field")[:160]
            f["label"] = label
            mapping = translator.translate(label, f.get("description") or None)
            semantic = mapping.semantic_type or "unknown"
            canonical = mapping.canonical_field
            fact = fact_by_semantic.get(semantic) if canonical else None
            status, explanation, preview, fid, fact_label = "UNSUPPORTED", "No safe Passport mapping is available for this field.", None, None, None
            if f.get("unsupported_reason"):
                explanation = str(f["unsupported_reason"])[:240]
            elif mapping.canonical_field is None:
                status = "AMBIGUOUS" if mapping.semantic_type == "address.unknown" or mapping.requires_user_confirmation else "UNSUPPORTED"
                explanation = mapping.explanation or "The field meaning is unclear; leave it blank or choose a compatible fact."
            elif not fact:
                status, explanation = "MISSING_FACT", "No matching fact exists in the Passport."
            elif not fact.get("user_confirmed") or fact.get("verification") not in permitted_status:
                status, explanation, fid = "PERMISSION_BLOCKED", "This Passport fact is not user verified, so it cannot be shared. Verify it first.", fact.get("id")
            elif origin not in json.loads(fact.get("permitted_origins") or "[]"):
                status, explanation, fid = "PERMISSION_BLOCKED", "This fact is not permitted for this destination. Change sharing permission separately in Passport settings.", fact.get("id")
            elif mapping.confidence < .70 or mapping.requires_user_confirmation:
                status, explanation, fid = "AMBIGUOUS", mapping.explanation or "Mapping needs your review.", fact.get("id")
            else:
                status, explanation, preview, fid, fact_label = "PROPOSED", mapping.explanation or "Mapped to an existing verified Passport fact.", fact["value"], fact["id"], fact.get("source_label")
            transform = canonical if canonical in {"first_name", "last_name"} else None
            if transform and fact and status in {"PROPOSED", "AMBIGUOUS"} and fact.get("user_confirmed") and fact.get("verification") in permitted_status and origin in json.loads(fact.get("permitted_origins") or "[]"):
                try:
                    preview = self._transformed_value(str(fact["value"]), transform)
                    fact_label = fact.get("source_label")
                except PreparationError as exc:
                    status, explanation, preview = "AMBIGUOUS", str(exc), None
            fields.append(FormField(
                field_id=str(f["field_id"]), fingerprint=str(f["fingerprint"]), field_type=str(f["field_type"]),
                label=label, name=str(f.get("name", ""))[:128], element_id=str(f.get("element_id", ""))[:128],
                placeholder=str(f.get("placeholder", ""))[:160], required=bool(f.get("required")),
                options=[str(x)[:160] for x in f.get("options", [])[:60]], constraints=f.get("constraints", {}),
                visible=bool(f.get("visible")), accessible_name=str(f.get("accessible_name", ""))[:160],
                description=str(f.get("description", ""))[:240], mapping_status=status, fact_id=fid,
                semantic_type=semantic,
                fact_label=fact_label, value_preview=preview, mapping_confidence=float(mapping.confidence),
                explanation=explanation, user_approved=False, value_transform=transform,
            ).model_dump())
        with self._db() as c:
            row = c.execute("SELECT * FROM application_preparations WHERE id=?", (preparation_id,)).fetchone()
            if not row:
                raise PreparationError("Preparation not found.", 404)
            if row["origin"] != origin or not self.origin_is_approved(origin):
                raise PreparationError("Destination approval changed. Start a new review.", 403)
            c.execute("UPDATE application_preparations SET fields_json=?,status='REVIEW_PENDING',approval_reference=NULL,receipt_json=NULL,updated_at=? WHERE id=?", (json.dumps(fields), _now(), preparation_id))
            self._event(c, preparation_id, "recovered" if recovery else "inspected", f"Inspected {len(fields)} safe fields; omitted {excluded_count} sensitive or unsupported controls.")
            result = c.execute("SELECT * FROM application_preparations WHERE id=?", (preparation_id,)).fetchone()
        return self._record(result)

    def review_field(self, preparation_id: str, field_id: str, decision: str, fact_id: str | None, current_facts: list[dict], origin: str) -> dict:
        state = self.get(preparation_id)
        if state["status"] != "REVIEW_PENDING":
            raise PreparationError("Field review is closed. Reinspect the destination to start a fresh review.", 409)
        facts = {f["id"]: f for f in current_facts}
        found = False
        for field in state["fields"]:
            if field["field_id"] != field_id:
                continue
            found = True
            if decision in {"approve", "map"}:
                proposed_id = field.get("fact_id")
                if decision == "approve" and fact_id is not None and fact_id != proposed_id:
                    raise PreparationError("A direct approval cannot substitute a different Passport fact. Use the explicit map action.", 422)
                chosen = facts.get(fact_id or field.get("fact_id"))
                if not chosen or not chosen.get("user_confirmed") or chosen.get("verification") not in {"USER_CONFIRMED"}:
                    raise PreparationError("Only an existing user-verified Passport fact can be mapped.", 422)
                if origin not in json.loads(chosen.get("permitted_origins") or "[]"):
                    raise PreparationError("This fact is not permitted for this destination. Update sharing permission separately.", 403)
                if field.get("field_type") in {"password", "file", "hidden", "captcha", "otp", "mfa", "radio", "checkbox", "radio-group", "checkbox-group"}:
                    raise PreparationError("This field type cannot be approved for automatic filling.", 422)
                if decision == "approve" and field["mapping_status"] != "PROPOSED":
                    raise PreparationError("Only a strong proposed mapping can be approved directly; explicitly map or leave this field blank.", 422)
                if decision == "approve" and chosen.get("semantic_type") != field.get("semantic_type"):
                    raise PreparationError("The Passport fact no longer matches the proposed field type.", 422)
                if decision == "map" and field.get("semantic_type") not in {"unknown", "address.unknown"} and chosen.get("semantic_type") != field.get("semantic_type"):
                    raise PreparationError("Choose a Passport fact with the proposed semantic type, or leave the field blank.", 422)
                value = self._transformed_value(str(chosen["value"]), field.get("value_transform"))
                field.update(mapping_status="APPROVED", fact_id=chosen["id"], fact_label=chosen.get("source_label"), value_preview=value, user_approved=True,
                             explanation="User explicitly approved this Passport fact for this destination.")
            elif decision == "reject":
                field.update(mapping_status="REJECTED", fact_id=None, fact_label=None, value_preview=None, user_approved=False)
            elif decision == "blank":
                field.update(mapping_status="BLANK", fact_id=None, fact_label=None, value_preview=None, user_approved=False,
                             explanation="User chose to leave this field blank.")
            break
        if not found:
            raise PreparationError("Inspected field not found. Inspect the page again.", 404)
        with self._db() as c:
            c.execute("UPDATE application_preparations SET fields_json=?,status='REVIEW_PENDING',approval_reference=NULL,updated_at=? WHERE id=?", (json.dumps(state["fields"]), _now(), preparation_id))
            self._event(c, preparation_id, "field_review", f"Field {field_id} decision: {decision}.")
        return self.get(preparation_id)

    def approve(self, preparation_id: str, approve: bool, current_facts: list[dict]) -> dict:
        state = self.get(preparation_id)
        if not approve:
            return state
        if state["status"] != "REVIEW_PENDING":
            raise PreparationError("Only a pending field review can be approved.", 409)
        if not self.origin_is_approved(state["origin"]):
            raise PreparationError("Destination origin approval is no longer active.", 403)
        facts = {f["id"]: f for f in current_facts}
        for field in state["fields"]:
            if field["mapping_status"] == "APPROVED":
                fact = facts.get(field.get("fact_id"))
                if not field.get("user_approved") or not fact or not fact.get("user_confirmed") or fact.get("verification") != "USER_CONFIRMED":
                    raise PreparationError("An approved mapping no longer passes Passport verification. Review mappings again.", 403)
                if state["origin"] not in json.loads(fact.get("permitted_origins") or "[]"):
                    raise PreparationError("An approved mapping is no longer permitted for this destination.", 403)
            elif field["mapping_status"] not in {"REJECTED", "BLANK", "UNSUPPORTED"}:
                raise PreparationError("Review every proposed field: approve, reject, map, or leave blank.", 409)
        approval_reference = str(uuid.uuid4())
        with self._db() as c:
            c.execute("UPDATE application_preparations SET status='PREPARATION_APPROVED',approval_reference=?,updated_at=? WHERE id=?", (approval_reference, _now(), preparation_id))
            self._event(c, preparation_id, "preparation_approved", "User approved field preparation. This does not submit the application.")
        return self.get(preparation_id)

    def fill_plan(self, preparation_id: str, current_url: str, inspected: list[dict], current_facts: list[dict]) -> tuple[dict, dict[str, str], dict[str, dict]]:
        state = self.get(preparation_id)
        if state["status"] != "PREPARATION_APPROVED" or not state.get("approval_reference"):
            raise PreparationError("Review and approve preparation before filling any fields.", 409)
        if not self.origin_is_approved(state["origin"]):
            raise PreparationError("Destination origin is no longer approved.", 403)
        if current_url.split("/", 3)[:3] != state["destination_url"].split("/", 3)[:3]:
            raise PreparationError("Browser destination changed. Inspect and review the application page again.", 403)
        fresh = {f["field_id"]: f for f in inspected}
        facts = {f["id"]: f for f in current_facts}
        values, approved_fields = {}, {}
        saved = {f["field_id"]: f for f in state["fields"]}
        for field_id, field in saved.items():
            if field["mapping_status"] != "APPROVED":
                continue
            page_field, fact = fresh.get(field_id), facts.get(field.get("fact_id"))
            if not page_field or page_field.get("fingerprint") != field["fingerprint"] or not page_field.get("visible"):
                raise PreparationError("A reviewed field changed or disappeared. Reinspect the page before filling.", 409)
            if page_field.get("unsupported_reason") or page_field.get("field_type") in {"password", "file", "hidden", "captcha", "otp", "mfa", "radio", "checkbox", "radio-group", "checkbox-group"}:
                raise PreparationError("A reviewed field is no longer safe to fill.", 403)
            if not fact or not fact.get("user_confirmed") or fact.get("verification") != "USER_CONFIRMED" or state["origin"] not in json.loads(fact.get("permitted_origins") or "[]"):
                raise PreparationError("Passport verification or sharing permission changed. Review the mapping again.", 403)
            values[field_id] = self._transformed_value(str(fact["value"]), field.get("value_transform"))
            approved_fields[field_id] = field
        return state, values, approved_fields

    def save_receipt(self, preparation_id: str, receipt: dict, status: str) -> dict:
        with self._db() as c:
            row = c.execute("SELECT * FROM application_preparations WHERE id=?", (preparation_id,)).fetchone()
            if not row:
                raise PreparationError("Preparation not found.", 404)
            c.execute("UPDATE application_preparations SET status=?,receipt_json=?,updated_at=? WHERE id=?", (status, json.dumps(receipt), _now(), preparation_id))
            c.execute("INSERT INTO receipts(id,origin,source_fact_id,source_version,source_field,destination_field,semantic_type,mapping_type,reason,confidence,permission,status,created_at) VALUES(?,?, '',0,?,?,?,?,?,?,?, ?,?)",
                      (receipt["id"], row["origin"], "Application preparation", f"{row['title']} · {len(receipt['successfully_filled_fields'])} fields prepared", "application.preparation", "PREPARATION_RECEIPT",
                       f"{receipt['approved_mapping_count']} approved; {len(receipt['skipped_fields'])} skipped; {len(receipt['failed_fields'])} failed. No submission occurred.", 1.0, "REVALIDATED", status, receipt["prepared_at"]))
            self._event(c, preparation_id, "fill_result", f"Prepared {len(receipt['successfully_filled_fields'])} field(s); {len(receipt['failed_fields'])} failed.")
        return self.get(preparation_id)

    def fail(self, preparation_id: str, detail: str) -> None:
        with self._db() as c:
            c.execute("UPDATE application_preparations SET status='FAILED',updated_at=? WHERE id=?", (_now(), preparation_id))
            self._event(c, preparation_id, "failed", detail)
