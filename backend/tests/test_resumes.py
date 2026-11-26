from __future__ import annotations

from io import BytesIO

import pytest
from docx import Document
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, DecodedStreamObject, NameObject

from backend.app import main
from backend.app.conversation.intents import interpret
from backend.app.conversation.schemas import ChatRequest
from backend.app.conversation.service import handle_message
from backend.app.resumes.extractor import MAX_RESUME_BYTES, ResumeInputError, parse_resume, validate_and_extract
from backend.app.resumes.schemas import ResumeItemUpdate
from backend.app.resumes.service import ResumeService


@pytest.fixture
def resume_service(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB", tmp_path / "resume-test.db")
    main.init()
    return ResumeService(main.db)


def make_docx():
    document = Document()
    for line in (
        "Kai Example", "kai.example@example.org", "+1 415 555 0134", "https://github.com/kaiexample",
        "EDUCATION", "Example University", "B.S. Computer Science", "Expected graduation: 2027",
        "SKILLS", "Python, FastAPI, SQLite, Docker",
        "PROJECTS", "Transit Planner — built a route planner using Python and public data.",
        "EXPERIENCE", "Example Labs — Engineering Intern (2024–2025)",
        "CERTIFICATIONS", "Cloud Foundations — Example Institute, 2025",
        "ACHIEVEMENTS", "Finalist, Campus Innovation Challenge (2025)",
    ):
        document.add_paragraph(line)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def make_text_pdf(text: str):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    operations = "BT /F1 12 Tf 40 740 Td " + " ".join(f"({line.replace('(', '').replace(')', '')}) Tj 0 -18 Td" for line in text.splitlines()) + " ET"
    stream = DecodedStreamObject()
    stream.set_data(operations.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_upload_checks_magic_extracts_text_and_detects_scanned_pdf():
    content = make_text_pdf("Kai Example\nkai@example.org\nSKILLS\nPython, SQL")
    filename, document = validate_and_extract("resume.pdf", content)
    assert filename == "resume.pdf"
    assert document.file_type == "PDF"
    assert "kai@example.org" in document.text
    assert any(item.category == "skills" for item in parse_resume(document.text))

    scanned = make_text_pdf("")
    with pytest.raises(ResumeInputError, match="scanned PDF"):
        validate_and_extract("scan.pdf", scanned)


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("resume.exe", b"MZ\x00", "Unsupported file type"),
        ("resume.pdf", b"PK\x03\x04not a pdf", "don’t match a PDF"),
        ("resume.docx", b"%PDF-not-word", "don’t match a DOCX"),
        ("resume.pdf", b"", "empty"),
        ("resume.pdf", b"%PDF-corrupted content", "uncorrupted PDF"),
    ],
)
def test_upload_rejects_unsupported_empty_or_corrupt_documents(filename, content, message):
    with pytest.raises(ResumeInputError, match=message):
        validate_and_extract(filename, content)


def test_upload_enforces_size_limit_and_does_not_trust_filename():
    with pytest.raises(ResumeInputError, match="8 MB or smaller"):
        validate_and_extract("resume.pdf", b"%PDF-" + b"x" * MAX_RESUME_BYTES)
    with pytest.raises(ResumeInputError, match="Choose a PDF or DOCX"):
        validate_and_extract("../", b"x")


def test_docx_parser_extracts_structured_candidates_without_verifying_them(resume_service):
    result = resume_service.upload("../Kai Example.docx", make_docx())
    assert result["resume"]["filename"] == "Kai Example.docx"
    assert result["resume"]["file_type"] == "DOCX"
    assert result["resume"]["item_count"] >= 9
    assert all(item["review_status"] == "EXTRACTED" for item in result["items"])
    by_label = {item["label"]: item for item in result["items"]}
    assert by_label["Email"]["value"] == "kai.example@example.org"
    assert "2027" in by_label["Graduation information"]["value"]
    assert by_label["Skills"]["details"]["skills"][:2] == ["Python", "FastAPI"]
    assert any(item["category"] == "project" for item in result["items"])
    assert result["warnings"]
    with main.db() as connection:
        assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        columns = {row[1] for row in connection.execute("PRAGMA table_info(resumes)")}
        assert "file_contents" not in columns
        assert "storage_path" not in columns


def test_extracts_sections_and_keeps_evidence_without_inventing_fields():
    items = parse_resume("""Kai Example
CONTACT
kai@example.org
EDUCATION
Example University
B.S. Computer Science
SKILLS
Python, Rust, SQL
PROJECTS
Transit Planner — built using Python.
EXPERIENCE
Example Labs — Engineering Intern, 2024–2025
PUBLICATIONS
No publications are listed.
""")
    assert {item.category for item in items} >= {"personal", "education", "skills", "project", "experience", "publication"}
    assert all(item.evidence for item in items)
    assert all(item.review_status == "EXTRACTED" for item in items)
    assert not any(item.category == "achievement" for item in items)


def test_review_edit_accept_duplicate_and_provenance_integration(resume_service):
    main.start()
    result = resume_service.upload("kai.docx", make_docx())
    resume_id = result["resume"]["id"]
    github = next(item for item in result["items"] if item["label"] == "GitHub")
    item_id = github["id"]
    edited = resume_service.update_item(resume_id, item_id, ResumeItemUpdate(value="https://github.com/kai-example"))
    assert edited["review_status"] == "EDITED"
    approved = resume_service.approve_items(resume_id, [item_id])
    item = approved["items"][0]
    assert item["review_status"] == "USER_VERIFIED"
    assert item["passport_fact_id"] == "github_url"
    duplicate = resume_service.approve_items(resume_id, [item_id])
    assert duplicate["items"][0]["review_status"] == "USER_VERIFIED"
    with main.db() as connection:
        fact = connection.execute("SELECT * FROM facts WHERE id='github_url'").fetchone()
        assert fact["value"] == "https://github.com/kai-example"
        assert fact["source_type"] == "resume_edited"
        assert fact["source_document_id"] == resume_id
        assert fact["source_section"] == "Contact"
        assert fact["source_evidence"] == "https://github.com/kaiexample"
        assert fact["extraction_confidence"] == .96
        assert fact["user_confirmed"] == 1
        assert fact["permitted_origins"] == "[]"
        assert connection.execute("SELECT count(*) FROM facts WHERE id='github_url'").fetchone()[0] == 1


def test_conflict_requires_existing_explicit_resolution_and_keeps_versions(resume_service):
    main.start()
    result = resume_service.upload("kai.docx", make_docx())
    name = next(item for item in result["items"] if item["label"] == "Possible name")
    accepted = resume_service.approve_items(result["resume"]["id"], [name["id"]])
    candidate = accepted["items"][0]
    assert candidate["review_status"] == "CONFLICT_PENDING"
    assert candidate["passport_version"] == 2
    with main.db() as connection:
        values = connection.execute("SELECT value,verification FROM facts WHERE id='full_name' ORDER BY version").fetchall()
        assert [row["value"] for row in values] == ["Aarav Mehta", "Kai Example"]
        assert values[1]["verification"] == "UNVERIFIED_CONFLICT"
    passport = main.state()["facts"]
    conflict = next(item for item in main.state()["conflicts"] if item["fact_id"] == "full_name")
    assert conflict["new_version"] == candidate["passport_version"]
    main.resolve(main.ResolveBody(fact_id="full_name", version=1))
    reviewed = resume_service.get_resume(result["resume"]["id"])
    chosen = next(item for item in reviewed["items"] if item["id"] == name["id"])
    assert chosen["review_status"] == "REJECTED"
    assert chosen["passport_version"] == 2
    assert any(fact["id"] == "full_name" and fact["version"] == 2 for fact in passport)


def test_reject_does_not_add_fact_and_resume_delete_removes_extraction_only(resume_service):
    main.start()
    result = resume_service.upload("kai.docx", make_docx())
    email = next(item for item in result["items"] if item["label"] == "Email")
    rejected = resume_service.reject_items(result["resume"]["id"], [email["id"]])
    assert rejected["items"][0]["review_status"] == "REJECTED"
    assert resume_service.delete_resume(result["resume"]["id"])
    assert resume_service.list_resumes() == []
    with main.db() as connection:
        assert connection.execute("SELECT count(*) FROM resume_items").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 8


def test_chat_reads_resume_and_passport_but_does_not_auto_approve(resume_service):
    uploaded = resume_service.upload("kai.docx", make_docx())
    skills_intent = interpret("Show me skills from my resume")
    assert skills_intent.tool_name == "get_resume_extraction"
    response = handle_message(ChatRequest(message="What information did you extract?"))
    assert response.tool_status == "completed"
    assert response.tool.tool_name == "get_resume_extraction"
    assert all(item["review_status"] == "EXTRACTED" for item in response.tool.data["items"])
    compare = handle_message(ChatRequest(message="I updated my resume. Compare it with my Passport."))
    assert compare.tool.tool_name == "compare_resume"
    assert "does not change" in compare.assistant_message
    query = interpret("Does my Passport contain my GitHub URL?")
    assert query.tool_name == "search_passport"
    adding = interpret("Add the verified projects from my resume to my Passport")
    assert adding.tool_name is None
    assert "won’t approve" in adding.reply
    assert resume_service.get_resume(uploaded["resume"]["id"])["items"]


def test_resume_endpoints_are_registered_with_typed_requests():
    paths = {route.path: route.methods for route in main.app.routes if getattr(route, "methods", None)}
    assert paths["/api/resumes/upload"] == {"POST"}
    assert paths["/api/resumes/{resume_id}/approve"] == {"POST"}
    assert paths["/api/resumes/{resume_id}/reject"] == {"POST"}
    schema = main.app.openapi()
    assert "multipart/form-data" in str(schema["paths"]["/api/resumes/upload"]["post"]["requestBody"])
