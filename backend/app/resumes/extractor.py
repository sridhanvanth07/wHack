from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse

from docx import Document
from pypdf import PdfReader

from .schemas import ResumeItem


MAX_RESUME_BYTES = 8 * 1024 * 1024
MAX_PDF_PAGES = 75
MAX_DOCUMENT_TEXT = 200_000
MIN_EXTRACTED_TEXT = 20


class ResumeInputError(ValueError):
    pass


@dataclass(frozen=True)
class ExtractedDocument:
    file_type: str
    text: str
    warnings: tuple[str, ...] = ()


_SECTION_NAMES: dict[str, str] = {
    "education": "education", "academic background": "education", "academics": "education",
    "skills": "skills", "technical skills": "skills", "technical expertise": "skills", "technologies": "skills",
    "projects": "project", "project experience": "project",
    "experience": "experience", "work experience": "experience", "professional experience": "experience",
    "internships": "experience", "employment": "experience",
    "certifications": "certification", "certificates": "certification", "licenses and certifications": "certification",
    "awards": "achievement", "achievements": "achievement", "honors": "achievement", "accomplishments": "achievement",
    "extracurricular activities": "activity", "activities": "activity", "leadership": "activity",
    "research": "research", "research experience": "research",
    "publications": "publication", "writing": "publication",
    "volunteering": "volunteering", "volunteer experience": "volunteering",
    "coursework": "coursework", "relevant coursework": "coursework",
}


def _check_filename(filename: str | None) -> tuple[str, str]:
    clean = PurePath((filename or "").replace("\\", "/")).name.strip()
    if not clean or clean in {".", ".."}:
        raise ResumeInputError("Choose a PDF or DOCX resume file.")
    suffix = PurePath(clean).suffix.casefold()
    if suffix not in {".pdf", ".docx"}:
        raise ResumeInputError("Unsupported file type. Upload a PDF or DOCX resume.")
    safe_name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", clean)[:180] or f"resume{suffix}"
    return safe_name, suffix[1:].upper()


def validate_and_extract(filename: str | None, content: bytes) -> tuple[str, ExtractedDocument]:
    safe_name, file_type = _check_filename(filename)
    if not content:
        raise ResumeInputError("The uploaded document is empty.")
    if len(content) > MAX_RESUME_BYTES:
        raise ResumeInputError("Resume files must be 8 MB or smaller.")
    if file_type == "PDF":
        document = _extract_pdf(content)
    else:
        document = _extract_docx(content)
    if len(document.text.strip()) < MIN_EXTRACTED_TEXT:
        raise ResumeInputError("We couldn’t extract readable text. This may be a scanned PDF; OCR is not available in this version.")
    return safe_name, document


def _extract_pdf(content: bytes) -> ExtractedDocument:
    if not content.startswith(b"%PDF-"):
        raise ResumeInputError("The file contents don’t match a PDF document.")
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise ResumeInputError("Password-protected PDFs aren’t supported. Upload an unlocked PDF.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ResumeInputError(f"PDF resumes may contain at most {MAX_PDF_PAGES} pages.")
        pages: list[str] = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
            if sum(map(len, pages)) > MAX_DOCUMENT_TEXT:
                raise ResumeInputError("The extracted document text is too long to review safely.")
        return ExtractedDocument("PDF", "\n".join(pages), ("The original resume file is processed in memory and is not retained.",))
    except ResumeInputError:
        raise
    except Exception as exc:
        raise ResumeInputError("The PDF could not be read. Check that it is a valid, uncorrupted PDF.") from exc


def _extract_docx(content: bytes) -> ExtractedDocument:
    if not content.startswith(b"PK\x03\x04"):
        raise ResumeInputError("The file contents don’t match a DOCX document.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(entry.file_size for entry in entries) > 16 * 1024 * 1024:
                raise ResumeInputError("The DOCX package expands beyond the safe processing limit.")
            if any(entry.flag_bits & 0x1 for entry in entries):
                raise ResumeInputError("Encrypted DOCX files aren’t supported.")
            if any(PurePath(entry.filename).is_absolute() or ".." in PurePath(entry.filename).parts for entry in entries):
                raise ResumeInputError("The DOCX package contains an unsafe file path.")
            if "word/document.xml" not in archive.namelist():
                raise ResumeInputError("The DOCX file is missing its document content.")
        document = Document(io.BytesIO(content))
        parts: list[str] = []
        for paragraph in document.paragraphs:
            if paragraph.text.strip():
                parts.append(paragraph.text)
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        text = "\n".join(parts)[:MAX_DOCUMENT_TEXT]
        return ExtractedDocument("DOCX", text, ("The original resume file is processed in memory and is not retained.",))
    except ResumeInputError:
        raise
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as exc:
        raise ResumeInputError("The DOCX could not be read. Check that it is a valid, uncorrupted Word document.") from exc
    except Exception as exc:
        raise ResumeInputError("The DOCX could not be read. Check that it is a valid, uncorrupted Word document.") from exc


def _clean_lines(text: str) -> list[str]:
    output = []
    for raw in text.replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" \t•●▪◦-*\u2022")
        if line:
            output.append(line[:600])
    return output


def _normalize_section_title(line: str) -> str | None:
    normalized = re.sub(r"[^a-z ]", "", line.casefold()).strip()
    return _SECTION_NAMES.get(normalized)


def _sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = [("Header", [])]
    for line in lines:
        category = _normalize_section_title(line)
        if category:
            label = line.strip(" :")[:100]
            sections.append((label, []))
        else:
            sections[-1][1].append(line)
    return sections


def _make_item(category: str, label: str, value: str, fact_id: str, semantic_type: str, section: str,
               evidence: str, confidence: float, details: dict[str, Any] | None = None) -> ResumeItem:
    item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "carryflow-resume\n" + "\n".join((category, section, label, value.casefold()))))
    return ResumeItem(id=item_id, category=category, label=label[:120], value=value[:4000], details=details or {},
                      target_fact_id=fact_id, semantic_type=semantic_type, section=section[:100],
                      evidence=evidence[:600], confidence=confidence)


def parse_resume(text: str) -> list[ResumeItem]:
    lines = _clean_lines(text)
    if not lines:
        return []
    sections = _sections(lines)
    items: list[ResumeItem] = []
    header = sections[0][1][:8]

    # Contact information is captured only when explicit syntax is present.
    for email in dict.fromkeys(re.findall(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.I)):
        items.append(_make_item("personal", "Email", email, "email", "person.email", "Contact", email, .99))
    for match in re.finditer(r"(?<!\w)(?:\+?\d[\d\s().-]{6,}\d)(?!\w)", text):
        phone = match.group(0).strip()
        digits = re.sub(r"\D", "", phone)
        if re.fullmatch(r"(?:19|20)\d{2}\s*[-–—]\s*(?:19|20)\d{2}", phone):
            continue
        if 7 <= len(digits) <= 15:
            items.append(_make_item("personal", "Phone", phone, "phone", "person.phone", "Contact", phone, .93))
            break
    link_fact_ids: set[str] = set()
    for match in re.finditer(r"(?:https?://)?(?:www\.)?(?:linkedin\.com|github\.com)/[^\s,;<>]+|https?://[^\s,;<>]+", text, re.I):
        value = match.group(0).rstrip(".)]}")
        if not value.lower().startswith(("http://", "https://")):
            value = "https://" + value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        host = parsed.hostname.casefold()
        if host == "linkedin.com" or host.endswith(".linkedin.com"):
            label, fact_id, sem = "LinkedIn", "linkedin_url", "professional.linkedin_url"
        elif host == "github.com" or host.endswith(".github.com"):
            label, fact_id, sem = "GitHub", "github_url", "professional.github_url"
        else:
            line = next((line for line in lines if value in line), "")
            if re.search(r"\b(portfolio|personal\s+website|website)\b", line, re.I):
                label, fact_id, sem = "Portfolio", "portfolio_url", "professional.portfolio_url"
            else:
                digest = hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()[:16]
                label, fact_id, sem = "Other professional link", f"resume_link_{digest}", "professional.link"
        if fact_id in link_fact_ids:
            continue
        link_fact_ids.add(fact_id)
        items.append(_make_item("professional_link", label, value, fact_id, sem, "Contact", match.group(0), .96))
    if header:
        first = header[0]
        if len(first) <= 90 and re.search(r"[A-Za-z]{2,}\s+[A-Za-z]{2,}", first) and not re.search(r"@|https?://|\d{3}", first):
            items.append(_make_item("personal", "Possible name", first, "full_name", "person.name", "Header", first, .62))

    for section, content in sections[1:]:
        category = _normalize_section_title(section) or "activity"
        evidence = "\n".join(content)
        if not content:
            continue
        if category == "skills":
            terms = list(dict.fromkeys(term.strip(" ·|/") for line in content for term in re.split(r"[,;|•·]", line) if term.strip(" ·|/")))
            if terms:
                value = ", ".join(terms[:80])
                items.append(_make_item("skills", "Skills", value, "skills", "skills.technical", section, evidence, .72, {"skills": terms[:80]}))
            continue
        if category == "education":
            institution_added = False
            degree_added = False
            graduation_added = False
            for line in content:
                lower = line.casefold()
                if not institution_added and re.search(r"(university|college|institute|school|polytechnic|academy)", lower):
                    items.append(_make_item("education", "Institution", line, "education_institution", "education.institution", section, line, .78, {"institution": line}))
                    institution_added = True
                if not degree_added and re.search(r"(bachelor|master|doctor|ph\.?d|b\.?\s?(?:tech|e|sc|com|ba)|m\.?\s?(?:tech|e|sc|com|ba)|diploma|degree)", lower):
                    degree_id, degree_sem = "education", "education.degree"
                    items.append(_make_item("education", "Degree or course", line, degree_id, degree_sem, section, line, .68, {"degree_or_course": line}))
                    degree_added = True
                if not graduation_added and re.search(r"(graduat|expected|class\s+of|passing|completion)", lower):
                    year_match = re.search(r"\b(?:19|20)\d{2}\b", line)
                    items.append(_make_item("education", "Graduation information", line, "graduation_year", "education.graduation_year", section, line, .66, {"year": year_match.group(0) if year_match else None}))
                    graduation_added = True
            continue
        if category == "experience":
            _extract_entries(items, content, section, "experience", "Experience detail", "career.experience")
            continue
        if category == "project":
            _extract_entries(items, content, section, "project", "Project detail", "career.project")
            continue
        if category == "certification":
            _extract_entries(items, content, section, "certification", "Certification", "career.certification")
            continue
        labels = {"achievement": "Achievement", "activity": "Activity", "research": "Research", "publication": "Publication", "volunteering": "Volunteering", "coursework": "Coursework"}
        _extract_entries(items, content, section, category, labels.get(category, "Resume detail"), f"career.{category}")

    # Keep one occurrence of each exact, explicitly extracted fact.
    unique: dict[tuple[str, str, str], ResumeItem] = {}
    for item in items:
        key = (item.category, item.target_fact_id, item.value.casefold())
        unique.setdefault(key, item)
    return list(unique.values())[:200]


def _extract_entries(items: list[ResumeItem], content: list[str], section: str, category: str,
                     label: str, semantic_type: str) -> None:
    lines = [line for line in content if line.strip()]
    # Preserve each source line verbatim as a reviewable fact; do not infer a role,
    # date, metric, or organization from neighboring text.
    for index, line in enumerate(lines[:60]):
        digest = hashlib.sha256((category + "\n" + line.casefold()).encode("utf-8")).hexdigest()[:16]
        fact_id = f"resume_{category}_{digest}"[:64]
        details: dict[str, Any] = {"source_line": line}
        date_match = re.search(r"\b(?:19|20)\d{2}\b(?:\s*[-–—to]+\s*(?:present|current|(?:19|20)\d{2}))?", line, re.I)
        if date_match:
            details["dates_as_written"] = date_match.group(0)
        tech_match = re.search(r"(?:technologies|tech stack|tools|built with)\s*[:—-]\s*(.+)$", line, re.I)
        if tech_match:
            details["technologies_as_written"] = [x.strip() for x in re.split(r"[,;|]", tech_match.group(1)) if x.strip()]
        confidence = .72 if index == 0 else .66
        items.append(_make_item(category, label, line, fact_id, semantic_type, section, line, confidence, details))
