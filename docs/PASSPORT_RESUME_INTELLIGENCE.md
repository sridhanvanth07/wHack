# Smart Passport 2.0 and Resume Intelligence

## Overview

The Resume tab accepts `.pdf` and `.docx` files up to 8 MB. Extraction is deterministic and local: PDF text is read with pypdf and DOCX paragraphs and tables with python-docx. The app does not send resume text to an LLM or external service. Scanned PDFs are not OCR processed.

## Review and Passport flow

Each extracted item includes a category, label, value, section, evidence excerpt, confidence, and review status. New items start as `EXTRACTED`; editing an item changes it to `EDITED`. Neither state is considered verified. Users can accept or reject one item, or accept/reject selected items. Accepted facts are marked user-confirmed, but their permitted sharing origins remain empty. Resume acceptance therefore does not grant permission to fill a portal.

When an extracted value differs from an existing Passport fact, CarryFlow creates the regular Passport conflict and leaves the resume item in `CONFLICT_PENDING` until it is resolved. Identical values link to the existing fact. Values and evidence are displayed as text, escaped in the UI, and are not treated as executable instructions.

Facts accepted from a resume record `source_type=resume` or `resume_edited`, the resume record ID, section, evidence excerpt, extraction confidence, and extraction time. Manual edits to Passport facts retain the normal manual-edit provenance. Removing a resume deletes its extraction/review rows; facts already accepted into Passport remain, along with their provenance excerpt.

## File handling and limits

The original upload is read into memory and discarded after parsing. SQLite stores the filename, format, upload time, content hash, extraction status, and extracted/reviewed items; it has no column for original file bytes or a storage path. Upload validation checks file type/signature, parser integrity, PDF encryption/page count, DOCX archive safety, and the 8 MB limit. PDF files are limited to 75 pages. Unsupported, malformed, encrypted, or text-empty documents return a clear error rather than being accepted as reviewed data.

The current extraction rules recognize common contact details, professional links, education, skills, projects, experience, and other labeled resume sections. Extraction is heuristic: confidence is an aid to review, not a measure of truth. The user must inspect evidence and explicitly accept facts. No sharing is enabled by accepting a fact.

## API

- `POST /api/resumes/upload` — multipart upload and extraction.
- `GET /api/resumes` — list resume metadata.
- `GET /api/resumes/{resume_id}` — resume and extracted review items.
- `PUT /api/resumes/{resume_id}/items/{item_id}` — edit a pending item.
- `POST /api/resumes/{resume_id}/approve` — accept selected pending items.
- `POST /api/resumes/{resume_id}/reject` — reject selected pending items.
- `DELETE /api/resumes/{resume_id}` — remove resume metadata and extraction items.

Chat can list and inspect extraction results and compare them with Passport. It cannot access the local file picker or approve extracted facts; use the Resume tab for upload and explicit review.
