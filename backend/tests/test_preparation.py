from __future__ import annotations

import socket

import pytest
from fastapi import HTTPException

from backend.app import main
from backend.app.automation.automator import FormAutomator, INSPECT_SCRIPT
from backend.app.conversation.schemas import ChatRequest
from backend.app.conversation.service import handle_message
from backend.app.discovery.schemas import Opportunity
from backend.app.opportunities.service import OpportunityService
from backend.app.passport import FactCreateBody, FactPermissionsBody
from backend.app.preparation.schemas import FieldReviewRequest, PreparationApprovalRequest
from backend.app.preparation.security import UnsafeDestination, validate_public_https_url
from backend.app.preparation.service import PreparationError, PreparationService
from backend.app.translator import FieldTranslator, MockMappingProvider
from backend.app.translator.schema import MappingResult


def _public_dns(monkeypatch, address="93.184.216.34"):
    monkeypatch.setattr("backend.app.preparation.security.socket.getaddrinfo", lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))])


def _run_immediate(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("The fake browser unexpectedly attempted asynchronous I/O")


def test_form_inspector_returns_only_bounded_metadata_and_stable_fingerprints():
    class Locator:
        async def evaluate_all(self, _script, _fingerprint):
            return None
    class Page:
        async def evaluate(self, script):
            assert script == INSPECT_SCRIPT
            return {"excluded_count": 4, "fields": [{
                "field_id": "cf-0-name", "field_type": "text", "name": "candidate", "element_id": "name",
                "label": "Full name", "required": True, "options": [], "unsupported_reason": "",
            }]}
        def locator(self, _selector):
            return Locator()
    result = _run_immediate(FormAutomator.inspect_fields(Page()))
    assert result["excluded_count"] == 4
    field = result["fields"][0]
    assert field["label"] == "Full name"
    assert len(field["fingerprint"]) == 64
    assert "value" not in field
    for sensitive_marker in ("password", "one.?time", "captcha", "access.?token", "cc-(number|csc|exp|name)", "submit"):
        assert sensitive_marker in INSPECT_SCRIPT


def test_form_filler_requires_reviewed_field_and_never_targets_submit():
    class Locator:
        def __init__(self, field_id):
            self.field_id = field_id
            self.value = ""
            self.first = self
        async def count(self):
            return 0 if self.field_id == "cf-missing" else 1
        async def get_attribute(self, name):
            return "reviewed-fingerprint" if name == "data-carryflow-fingerprint" else None
        async def is_visible(self):
            return True
        async def evaluate(self, script):
            return "input" if "tagName" in script else "text"
        async def fill(self, value):
            self.value = value
        async def select_option(self, **_kwargs):
            raise AssertionError("A text control should not use select_option")
    class Page:
        def __init__(self):
            self.locators = {}
        def locator(self, selector):
            field_id = selector.split('"')[1] if 'data-carryflow-field-id' in selector else selector.lstrip('#')
            return self.locators.setdefault(field_id, Locator(field_id))
        async def evaluate(self, _script):
            return {key: value.value for key, value in self.locators.items() if value.value}
    page = Page()
    outcome = _run_immediate(FormAutomator.fill_fields(page, {"cf-name": "Kai Example", "cf-missing": "x"}, {"cf-name": "reviewed-fingerprint"}))
    assert outcome["filled"] == ["cf-name"]
    assert "cf-missing" in outcome["errors"]
    assert "submit-application" not in outcome["filled"]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///C:/Windows/win.ini", "data:text/html,hello", "http://example.com/app",
    "https://user:pass@example.com/app", "https://example.com:8443/app", "https://localhost/app",
    "https://127.0.0.1/app", "https://192.168.1.2/app", "https://10.0.0.3/app", "https://metadata.google.internal/",
])
def test_unsafe_application_urls_are_rejected(monkeypatch, url):
    _public_dns(monkeypatch)
    with pytest.raises(UnsafeDestination):
        validate_public_https_url(url)


def test_application_url_strips_credentials_and_keeps_only_public_https(monkeypatch):
    _public_dns(monkeypatch)
    origin, url = validate_public_https_url("https://Jobs.Example.com/app?token=abc&ref=source#form")
    assert origin == "https://jobs.example.com"
    assert url == "https://jobs.example.com/app?ref=source"


def test_dns_resolution_rejects_mixed_public_and_private_addresses(monkeypatch):
    rows = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
    ]
    monkeypatch.setattr("backend.app.preparation.security.socket.getaddrinfo", lambda *args, **kwargs: rows)
    with pytest.raises(UnsafeDestination, match="Private"):
        validate_public_https_url("https://example.org/app")


@pytest.fixture
def preparation_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", tmp_path / "preparation.db")
    main.init()
    yield


def _saved_opportunity():
    item = Opportunity(
        id="fixture-job", title="Software Intern", company_name="Example Labs",
        listing_url="https://jobs.example.com/intern/app?token=must-not-persist&ref=demo", source_name="Synthetic",
    )
    service = OpportunityService(main.db)
    service.remember_search("prep-test", [item])
    return service.save_from_context("prep-test", item.id)[0]


def _start_service_flow(monkeypatch, record):
    _public_dns(monkeypatch)
    service = PreparationService(main.db)
    origin, destination = validate_public_https_url(record["opportunity"]["listing_url"])
    with pytest.raises(PreparationError) as denied:
        service.create(record["id"], record["opportunity"], destination, origin)
    assert denied.value.status_code == 403
    service.approve_origin(origin)
    with main.db() as c:
        main.add_or_version_fact(c, FactCreateBody(id="full_name", label="Full name", value="Kai Example", permitted_origins=[origin]))
    state = service.create(record["id"], record["opportunity"], destination, origin)
    inspected = [{
        "field_id": "cf-0-name", "fingerprint": "stable-field-signature", "field_type": "text",
        "label": "Full name", "accessible_name": "Full name", "name": "candidate", "element_id": "candidate",
        "placeholder": "", "required": True, "options": [], "constraints": {}, "visible": True,
        "description": "", "unsupported_reason": "",
    }]
    facts = [dict(row) for row in main.get_current_facts(main.db()).values()]
    reviewed = service.set_inspection(state["id"], inspected, facts, FieldTranslator(MockMappingProvider()), origin, excluded_count=2)
    return service, origin, inspected, facts, reviewed


def test_saved_opportunity_preview_and_origin_gate(preparation_db, monkeypatch):
    _public_dns(monkeypatch)
    record = _saved_opportunity()
    preview = main.preview_preparation(record["id"])
    assert preview["origin"] == "https://jobs.example.com"
    assert not preview["origin_approved"]
    assert "token=must-not-persist" not in preview["destination_url"]
    service = PreparationService(main.db)
    with pytest.raises(PreparationError):
        service.create(record["id"], record["opportunity"], preview["destination_url"], preview["origin"])


def test_review_approval_rechecks_mapping_permission_and_creates_safe_receipt(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    field_id = prep["fields"][0]["field_id"]
    assert prep["fields"][0]["mapping_status"] == "PROPOSED"
    with pytest.raises(PreparationError, match="Review every proposed field"):
        service.approve(prep["id"], True, facts)
    with pytest.raises(PreparationError, match="cannot substitute"):
        service.review_field(prep["id"], field_id, "approve", "email", facts, origin)
    reviewed = service.review_field(prep["id"], field_id, "approve", None, facts, origin)
    approved = service.approve(prep["id"], True, facts)
    assert approved["status"] == "PREPARATION_APPROVED"
    _, values, approved_fields = service.fill_plan(prep["id"], approved["destination_url"], inspected, facts)
    assert values == {field_id: "Kai Example"}
    assert field_id in approved_fields

    main.update_passport_permissions("full_name", FactPermissionsBody(permitted_origins=[]))
    with pytest.raises(PreparationError, match="permission changed"):
        service.fill_plan(prep["id"], approved["destination_url"], inspected, [dict(row) for row in main.get_current_facts(main.db()).values()])


def test_first_and_last_name_are_derived_from_verified_full_name(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    inspected = [dict(inspected[0], label="First name", accessible_name="First name")]
    pending = service.set_inspection(prep["id"], inspected, facts, FieldTranslator(MockMappingProvider()), origin)
    field = pending["fields"][0]
    assert field["mapping_status"] == "AMBIGUOUS"
    assert field["value_preview"] == "Kai"
    assert field["value_transform"] == "first_name"
    mapped = service.review_field(prep["id"], field["field_id"], "map", field["fact_id"], facts, origin)
    approved = service.approve(prep["id"], True, facts)
    _, values, _ = service.fill_plan(approved["id"], approved["destination_url"], inspected, facts)
    assert values == {field["field_id"]: "Kai"}


def test_single_word_name_is_not_guessed_as_first_name(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    facts = [dict(facts[0], value="Kai")]
    inspected = [dict(inspected[0], label="First name", accessible_name="First name")]
    pending = service.set_inspection(prep["id"], inspected, facts, FieldTranslator(MockMappingProvider()), origin)
    assert pending["fields"][0]["mapping_status"] == "AMBIGUOUS"
    assert pending["fields"][0]["value_preview"] is None
    with pytest.raises(PreparationError, match="cannot be safely split"):
        service.review_field(prep["id"], pending["fields"][0]["field_id"], "map", facts[0]["id"], facts, origin)


def test_changed_field_and_destination_fail_revalidation(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    service.review_field(prep["id"], "cf-0-name", "approve", None, facts, origin)
    approved = service.approve(prep["id"], True, facts)
    changed = [dict(inspected[0], fingerprint="changed-signature")]
    with pytest.raises(PreparationError, match="field changed"):
        service.fill_plan(prep["id"], approved["destination_url"], changed, facts)
    with pytest.raises(PreparationError, match="Browser destination changed"):
        service.fill_plan(prep["id"], "https://other.example/form", inspected, facts)


def test_recovery_clears_prior_mapping_approval(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    service.review_field(prep["id"], "cf-0-name", "approve", None, facts, origin)
    approved = service.approve(prep["id"], True, facts)
    recovered = service.set_inspection(prep["id"], inspected, facts, FieldTranslator(MockMappingProvider()), origin, recovery=True)
    assert recovered["status"] == "REVIEW_PENDING"
    assert recovered["approval_reference"] is None
    assert recovered["fields"][0]["mapping_status"] == "PROPOSED"
    with pytest.raises(PreparationError, match="approve preparation"):
        service.fill_plan(prep["id"], approved["destination_url"], inspected, facts)


def test_unverified_and_low_confidence_facts_are_not_auto_proposed(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    unverified = [dict(facts[0], verification="UNVERIFIED", user_confirmed=0)]
    result = service.set_inspection(prep["id"], inspected, unverified, FieldTranslator(MockMappingProvider()), origin)
    assert result["fields"][0]["mapping_status"] == "PERMISSION_BLOCKED"
    assert not result["fields"][0]["user_approved"]

    class LowConfidenceTranslator:
        def translate(self, label, description=None):
            return MappingResult(original_label=label, canonical_field="full_name", semantic_type="person.name", fact_id="full_name", confidence=.4, explanation="Weak proposal", requires_user_confirmation=True)
    weak = service.set_inspection(prep["id"], inspected, facts, LowConfidenceTranslator(), origin)
    assert weak["fields"][0]["mapping_status"] == "AMBIGUOUS"


def test_receipt_contains_counts_not_profile_values(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    field_id = prep["fields"][0]["field_id"]
    service.review_field(prep["id"], field_id, "approve", None, facts, origin)
    service.approve(prep["id"], True, facts)
    receipt = {
        "id": "receipt-1", "workflow_id": prep["id"], "opportunity_id": prep["opportunity_id"], "origin": origin,
        "prepared_at": "2026-11-26T00:00:00+00:00", "detected_field_count": 1, "approved_mapping_count": 1,
        "successfully_filled_fields": ["Full name"], "skipped_fields": [], "permission_blocked_fields": [],
        "ambiguous_fields": [], "failed_fields": {}, "recovery_state": "PREPARED", "user_approval_reference": "approval-id",
    }
    stored = service.save_receipt(prep["id"], receipt, "PREPARED")
    assert stored["receipt"] == receipt
    assert "Kai Example" not in str(stored["receipt"])
    with main.db() as c:
        row = c.execute("SELECT * FROM receipts WHERE id='receipt-1'").fetchone()
        assert row["mapping_type"] == "PREPARATION_RECEIPT"
        assert "Kai Example" not in row["reason"]
    response = handle_message(ChatRequest(message="What was filled?"))
    assert response.tool_status == "completed"
    assert "Full name" in response.assistant_message
    assert "Kai Example" not in response.assistant_message


def test_partial_receipt_records_failures_without_claiming_success(preparation_db, monkeypatch):
    service, origin, inspected, facts, prep = _start_service_flow(monkeypatch, _saved_opportunity())
    receipt = {
        "id": "partial-receipt", "workflow_id": prep["id"], "opportunity_id": prep["opportunity_id"], "origin": origin,
        "prepared_at": "2026-11-26T00:00:00+00:00", "detected_field_count": 2, "approved_mapping_count": 1,
        "successfully_filled_fields": [], "skipped_fields": ["Phone"], "permission_blocked_fields": ["Phone"],
        "ambiguous_fields": [], "failed_fields": {"Email": "Field changed during preparation."},
        "recovery_state": "PARTIALLY_PREPARED", "user_approval_reference": "approval-id",
    }
    stored = service.save_receipt(prep["id"], receipt, "PARTIALLY_PREPARED")
    assert stored["status"] == "PARTIALLY_PREPARED"
    assert stored["receipt"]["successfully_filled_fields"] == []
    assert stored["receipt"]["failed_fields"] == {"Email": "Field changed during preparation."}


def test_preparation_routes_are_typed_and_narrowly_scoped(preparation_db):
    routes = {route.path for route in main.app.routes}
    assert "/api/preparations/start" in routes
    assert "/api/preparations/{preparation_id}/fields/{field_id}/review" in routes
    assert "/api/preparations/{preparation_id}/fill" in routes
    assert "/api/preparations/{preparation_id}/receipt" in routes
    assert "/api/preparations/{preparation_id}/recover" in routes
    assert "/api/preparations/origins/approve" in routes
    assert main.app.openapi()["paths"]["/api/preparations/start"]["post"]["requestBody"]
