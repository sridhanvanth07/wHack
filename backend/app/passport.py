from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from backend.app.translator.canonical import (
    CANONICAL_FIELDS,
    FACT_ID_TO_CANONICAL,
    CanonicalFieldDefinition,
)

NOW = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
DEFAULT_ALLOWED = ["http://127.0.0.1:8000", "http://localhost:8000"]


# --- Pydantic Schemas ---

class FactCreateBody(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1)
    semantic_type: Optional[str] = None
    source_url: str = "http://127.0.0.1:8000/portal/source"
    source_label: str = "User Profile"
    permitted_origins: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED))
    purpose: str = "internship and job applications"
    user_confirmed: bool = True
    is_conflict: bool = False
    source_type: Literal["manual", "resume", "resume_edited"] = "manual"
    source_document_id: Optional[str] = Field(default=None, max_length=64)
    source_section: Optional[str] = Field(default=None, max_length=100)
    source_evidence: Optional[str] = Field(default=None, max_length=600)
    extraction_confidence: Optional[float] = Field(default=None, ge=0, le=1)


class FactUpdateBody(BaseModel):
    value: str = Field(min_length=1)
    source_label: Optional[str] = "User update"
    permitted_origins: Optional[list[str]] = None
    purpose: Optional[str] = None
    user_confirmed: bool = True


class FactConfirmBody(BaseModel):
    version: Optional[int] = None


class FactPermissionsBody(BaseModel):
    permitted_origins: list[str]
    purpose: Optional[str] = None


# --- Validation Rules ---

def validate_email(email: str) -> None:
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    if not re.match(pattern, email.strip()):
        raise HTTPException(422, f"Invalid email format: '{email}'")


def validate_phone(phone: str) -> None:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 7 or len(digits) > 15:
        raise HTTPException(422, f"Invalid phone number: must contain 7-15 digits (got '{phone}')")
    if not re.match(r"^[\d\s+\-().]+$", phone.strip()):
        raise HTTPException(422, f"Invalid phone number characters in '{phone}'")


def validate_fact_data(fact_id: str, value: str, semantic_type: Optional[str]) -> str:
    """Validates data integrity and ensures correct semantic types."""
    val = value.strip()
    if not val:
        raise HTTPException(422, "Fact value cannot be empty")

    fid = fact_id.strip().lower()
    sem = semantic_type.strip().lower() if semantic_type else None

    # Derive default semantic type from canonical fields if known
    canon_def = CANONICAL_FIELDS.get(fid) or FACT_ID_TO_CANONICAL.get(fid)
    if canon_def:
        canon_sem = canon_def.semantic_type
        if sem and sem != canon_sem:
            # Prevent address confusion
            if fid in {"current_address", "address.current"} and "permanent" in sem:
                raise HTTPException(422, "Cannot assign permanent address semantic type to current_address")
            if fid in {"permanent_address", "address.permanent"} and "current" in sem:
                raise HTTPException(422, "Cannot assign current address semantic type to permanent_address")
        sem = canon_sem
    elif not sem:
        sem = f"custom.{fid}"

    # Format validations
    if fid == "email" or sem == "person.email":
        validate_email(val)
    elif fid == "phone" or sem == "person.phone":
        validate_phone(val)

    return sem


# --- Service Layer ---

def list_passport_facts(c) -> dict[str, Any]:
    """Returns all current facts, historical versions, and available canonical templates."""
    all_rows = [dict(r) for r in c.execute("SELECT * FROM facts ORDER BY id, version DESC").fetchall()]
    conflicts = [dict(r) for r in c.execute("SELECT * FROM conflicts WHERE resolved=0").fetchall()]

    # Group by id to identify active vs historical versions
    by_id: dict[str, list[dict[str, Any]]] = {}
    for r in all_rows:
        by_id.setdefault(r["id"], []).append(r)

    resolved = {
        row["fact_id"]: row["resolution_version"]
        for row in c.execute("SELECT fact_id, resolution_version FROM conflicts WHERE resolved=1").fetchall()
    }

    active_facts = []
    for fid, versions in by_id.items():
        chosen = None
        if fid in resolved and resolved[fid] is not None:
            chosen = next((v for v in versions if v["version"] == resolved[fid]), None)
        if not chosen:
            non_superseded = [v for v in versions if v.get("verification") != "SUPERSEDED"]
            chosen = non_superseded[0] if non_superseded else versions[0]
        active_facts.append({
            "active": chosen,
            "history": versions,
            "version_count": len(versions),
            "has_conflict": any(c["fact_id"] == fid for c in conflicts),
        })

    canonical_templates = [
        {
            "id": cf.fact_id,
            "canonical_name": cf.name,
            "label": cf.label,
            "semantic_type": cf.semantic_type,
            "description": cf.description,
            "example": cf.example,
        }
        for cf in CANONICAL_FIELDS.values()
    ]

    return {
        "passport": active_facts,
        "total_facts": len(active_facts),
        "canonical_templates": canonical_templates,
        "default_allowed_origins": DEFAULT_ALLOWED,
    }


def add_or_version_fact(c, body: FactCreateBody) -> dict[str, Any]:
    fid = body.id.strip().lower()
    sem = validate_fact_data(fid, body.value, body.semantic_type)
    val = body.value.strip()

    # Check existing versions
    existing = c.execute("SELECT * FROM facts WHERE id=? ORDER BY version DESC", (fid,)).fetchall()
    now_iso = NOW()

    if existing:
        latest = existing[0]
        ver = latest["version"] + 1

        if body.is_conflict:
            verification = "UNVERIFIED_CONFLICT"
            user_confirmed = 0
            last_confirmed = None
            c.execute("INSERT INTO conflicts VALUES(?,?,?,?,0,NULL)", (str(uuid.uuid4()), fid, latest["version"], ver))
        elif body.user_confirmed:
            verification = "USER_CONFIRMED"
            user_confirmed = 1
            last_confirmed = now_iso
            # Mark previous unverified conflict versions as superseded
            c.execute("UPDATE facts SET verification='SUPERSEDED' WHERE id=? AND verification='UNVERIFIED_CONFLICT'", (fid,))
            c.execute("UPDATE conflicts SET resolved=1,resolution_version=? WHERE fact_id=? AND resolved=0", (ver, fid))
        else:
            verification = "UNVERIFIED"
            user_confirmed = 0
            last_confirmed = None
    else:
        ver = 1
        user_confirmed = 1 if body.user_confirmed else 0
        verification = "USER_CONFIRMED" if user_confirmed else "UNVERIFIED"
        last_confirmed = now_iso if user_confirmed else None

    c.execute(
        "INSERT INTO facts(id, key, semantic_type, value, source_url, source_label, collected_at, version, confidence, verification, permitted_origins, purpose, user_confirmed, last_confirmed_at, source_type, source_document_id, source_section, source_evidence, extraction_confidence, extracted_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            fid,
            sem,
            val,
            body.source_url,
            body.label,
            now_iso,
            ver,
            0.99 if user_confirmed else 0.85,
            verification,
            json.dumps(sorted(set(body.permitted_origins))),
            body.purpose,
            user_confirmed,
            last_confirmed,
            body.source_type,
            body.source_document_id,
            body.source_section,
            body.source_evidence,
            body.extraction_confidence,
            now_iso if body.source_type in {"resume", "resume_edited"} else None,
        ),
    )

    return {"ok": True, "id": fid, "version": ver, "verification": verification, "user_confirmed": user_confirmed}


def update_fact_value(c, fact_id: str, body: FactUpdateBody) -> dict[str, Any]:
    fid = fact_id.strip().lower()
    existing = c.execute("SELECT * FROM facts WHERE id=? ORDER BY version DESC", (fid,)).fetchall()
    if not existing:
        raise HTTPException(404, f"Fact with id '{fid}' not found in passport")

    latest = existing[0]
    sem = validate_fact_data(fid, body.value, latest["semantic_type"])
    val = body.value.strip()

    origins = body.permitted_origins if body.permitted_origins is not None else json.loads(latest["permitted_origins"])
    purpose = body.purpose if body.purpose is not None else latest["purpose"]
    source_label = body.source_label or latest["source_label"]
    now_iso = NOW()

    # If value is unchanged, just update metadata on latest version
    if val == latest["value"]:
        user_confirmed = 1 if body.user_confirmed else latest["user_confirmed"]
        last_confirmed = now_iso if user_confirmed else latest["last_confirmed_at"]
        c.execute(
            "UPDATE facts SET permitted_origins=?, purpose=?, source_label=?, user_confirmed=?, last_confirmed_at=? WHERE id=? AND version=?",
            (json.dumps(sorted(set(origins))), purpose, source_label, user_confirmed, last_confirmed, fid, latest["version"]),
        )
        return {"ok": True, "id": fid, "version": latest["version"], "action": "updated_metadata"}

    # Value has changed: create a new version to preserve provenance and detect drift
    ver = latest["version"] + 1
    user_confirmed = 1 if body.user_confirmed else 0
    verification = "USER_CONFIRMED" if user_confirmed else "UNVERIFIED"
    last_confirmed = now_iso if user_confirmed else None

    # Mark earlier conflicts resolved with this user-confirmed update
    if user_confirmed:
        c.execute("UPDATE facts SET verification='SUPERSEDED' WHERE id=? AND verification='UNVERIFIED_CONFLICT'", (fid,))
        c.execute("UPDATE conflicts SET resolved=1,resolution_version=? WHERE fact_id=? AND resolved=0", (ver, fid))

    c.execute(
        "INSERT INTO facts(id, key, semantic_type, value, source_url, source_label, collected_at, version, confidence, verification, permitted_origins, purpose, user_confirmed, last_confirmed_at, source_type, source_document_id, source_section, source_evidence, extraction_confidence, extracted_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            fid,
            fid,
            sem,
            val,
            latest["source_url"],
            source_label,
            now_iso,
            ver,
            0.99 if user_confirmed else 0.85,
            verification,
            json.dumps(sorted(set(origins))),
            purpose,
            user_confirmed,
            last_confirmed,
            "manual_edit",
            None,
            None,
            None,
            None,
            None,
        ),
    )

    return {"ok": True, "id": fid, "version": ver, "action": "created_new_version", "verification": verification}


def confirm_fact_version(c, fact_id: str, version: Optional[int] = None) -> dict[str, Any]:
    fid = fact_id.strip().lower()
    if version is not None:
        row = c.execute("SELECT * FROM facts WHERE id=? AND version=?", (fid, version)).fetchone()
    else:
        row = c.execute("SELECT * FROM facts WHERE id=? ORDER BY version DESC LIMIT 1", (fid,)).fetchone()

    if not row:
        raise HTTPException(404, f"Fact '{fid}' (version {version}) not found")

    target_ver = row["version"]
    now_iso = NOW()

    c.execute(
        "UPDATE facts SET verification='USER_CONFIRMED', user_confirmed=1, last_confirmed_at=? WHERE id=? AND version=?",
        (now_iso, fid, target_ver),
    )
    # Also resolve any unresolved conflicts pointing to this fact
    c.execute(
        "UPDATE conflicts SET resolved=1, resolution_version=? WHERE fact_id=? AND resolved=0",
        (target_ver, fid),
    )

    return {"ok": True, "id": fid, "version": target_ver, "verification": "USER_CONFIRMED", "last_confirmed_at": now_iso}


def update_fact_permissions(c, fact_id: str, body: FactPermissionsBody) -> dict[str, Any]:
    fid = fact_id.strip().lower()
    latest = c.execute("SELECT * FROM facts WHERE id=? ORDER BY version DESC LIMIT 1", (fid,)).fetchone()
    if not latest:
        raise HTTPException(404, f"Fact '{fid}' not found")

    origins_json = json.dumps(sorted(set(body.permitted_origins)))
    if body.purpose is not None:
        c.execute("UPDATE facts SET permitted_origins=?, purpose=? WHERE id=?", (origins_json, body.purpose, fid))
    else:
        c.execute("UPDATE facts SET permitted_origins=? WHERE id=?", (origins_json, fid))

    return {"ok": True, "id": fid, "permitted_origins": body.permitted_origins}
