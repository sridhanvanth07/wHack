# CarryFlow — AI-Powered Browser Task Assistant

> **Beyond the browser.** CarryFlow is a local-first cross-website workflow assistant with a persistent Smart Personal Data Passport, AI-powered field translation, conflict detection, and Playwright-based real browser automation.

---

##  Project Structure

```
wHack/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app — all API endpoints
│   │   ├── passport.py          # Smart Personal Data Passport (CRUD, versioning, permissions)
│   │   ├── translator/          # AI Field Translator
│   │   │   ├── canonical.py     # 8 canonical field definitions
│   │   │   ├── providers.py     # MockMappingProvider + LLMMappingProvider
│   │   │   ├── schema.py        # Pydantic schemas (MappingResult, RawLLMResponse)
│   │   │   ├── service.py       # FieldTranslator (validation, hallucination guard)
│   │   │   └── __init__.py
│   │   ├── automation/          # Shared Playwright browser and form automation
│   │   │   ├── manager.py       # PlaywrightManager (Chromium lifecycle, origin guard)
│   │   │   ├── automator.py     # FormAutomator (inspect, fill, observe — never submits)
│   │   │   └── __init__.py
│   │   └── preparation/         # Phase 5: saved-opportunity application preparation
│   │       ├── security.py      # HTTPS/DNS destination checks
│   │       ├── schemas.py       # typed review, state, and receipt models
│   │       └── service.py       # origin approvals and preparation workflow
│   └── tests/
│       ├── test_carryflow.py    # 9 core workflow tests
│       ├── test_translator.py   # 12 AI field translator tests
│       ├── test_passport.py     # 8 Smart Passport tests
│       ├── test_demo_audit.py   # 2 end-to-end audit tests
│       └── test_playwright_manager.py  # 3 Playwright automation tests
├── frontend/
│   ├── index.html               # Dashboard shell (3-column layout)
│   ├── app.js                   # All UI logic (render, fill, resolve, passport modals)
│   ├── app.css                  # Dashboard + Passport styles
│   ├── portal.css               # Shared portal styles
│   ├── portal-variants.css      # Portal A / B brand variants
│   └── goal.css                 # Goal card styles
├── docs/
│   ├── ARCHITECTURE.md
│   ├── COMPETITIVE_RESEARCH.md
│   ├── DEMO_SCRIPT.md
│   ├── INNOVATION.md
│   ├── PRODUCT_SPEC.md
│   └── SECURITY.md
├── .vscode/
│   ├── launch.json              # Run server + tests with F5
│   ├── settings.json            # Python interpreter, test discovery
│   ├── tasks.json               # Ctrl+Shift+B tasks
│   └── extensions.json          # Recommended extensions
├── .env.example                 # Copy to .env and fill in keys
├── .gitignore
├── pytest.ini
├── requirements.txt
└── README.md
```

---

##  Quick Start

### Prerequisites
- Python 3.11 or higher
- PowerShell (Windows) or Bash (Mac/Linux)

### 1. Clone & Setup

```powershell
# Clone the repo
git clone <your-repo-url>
cd wHack

# Create virtual environment
python -m venv .venv

# Activate it (Windows)
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```powershell
# Copy the example config
Copy-Item .env.example .env

# .env works as-is for the demo (mock AI mode, no API key needed)
# To use a real LLM, edit .env:
#   LLM_PROVIDER=gemini
#   LLM_API_KEY=your-key-here
```

### 3. Run the App

```powershell
uvicorn backend.app.main:app --reload
```

Open **http://127.0.0.1:8000** in your browser.

The chat tab can also search internship listings. Remotive works without credentials; optional Adzuna India results require server-side `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`. See [docs/DISCOVERY.md](docs/DISCOVERY.md) for source coverage, attribution, and limits.

Save search results from their cards or use the **Saved** dashboard tab to manage listings, application statuses, history, deadlines, and private notes. See [docs/OPPORTUNITY_TRACKING.md](docs/OPPORTUNITY_TRACKING.md) for storage and API details.

Use the **Resume** tab to upload a PDF or DOCX resume (up to 8 MB), review evidence-backed extracted items, edit them, and accept selected facts into Smart Passport. Files are processed in memory and discarded; scanned PDFs require OCR and are not supported. See [docs/PASSPORT_RESUME_INTELLIGENCE.md](docs/PASSPORT_RESUME_INTELLIGENCE.md) for privacy, parsing, provenance, and API details.

In **Saved**, choose **Prepare application** to preview a saved listing destination, explicitly approve its origin, inspect form fields, review Passport mappings, and prepare only approved fields in the existing Playwright browser. Passport sharing permission for each external origin is a separate action. Submission, credentials, CAPTCHA/MFA, and resume-file upload are not automated. See [docs/APPLICATION_PREPARATION.md](docs/APPLICATION_PREPARATION.md) for the security model and endpoints.

---

##  Running Tests

```powershell
# Run all tests
.\.venv\Scripts\pytest -v

# Run a specific test file
.\.venv\Scripts\pytest backend/tests/test_carryflow.py -v

# Run with detailed output on failure
.\.venv\Scripts\pytest -v --tb=long
```

**Current verification:** 126 passing tests and 3 skipped browser-dependent checks; the live browser end-to-end suite is run separately.

---

##  Demo Walkthrough

1. **Start demo** — CarryFlow loads 8 synthetic profile facts (Aarav Mehta) into the Passport.
2. **Navigate to Portal A** (Northstar Internships) → **Prepare this portal** — AI Field Translator maps differently-labelled fields to canonical profile fields.
3. **Fill approved fields** — Only confirmed, permitted facts are written. Ambiguous fields are held for review.
4. **Navigate to Portal B** (Summit Talent) — Different labels (`highest_qualification`, `passing_year`, `core_competencies`) auto-translate to the same canonical facts. The generic `location` field is flagged as ambiguous.
5. **Introduce phone conflict** — Creates a versioned fact conflict. Resolve it in the Conflict card (choose v1 or v2).
6. **Interrupt & resume** — CarryFlow reads live page values and reconciles them with the action ledger before replaying any fills.
7. **Prepare submission → Approve** — Two-step submission. No data is sent externally.

> Reset anytime with **Reset demo**.

---

##  Configuration

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `mock` | `mock`, `gemini`, `openai`, `anthropic` |
| `LLM_API_KEY` | _(empty)_ | API key for LLM provider |
| `LLM_MODEL` | _(auto)_ | Model name override |
| `DATABASE_URL` | `sqlite:///./carryflow.db` | SQLite database path |
| `BROWSER_MODE` | `playwright` | `playwright` or `iframe` |
| `PLAYWRIGHT_HEADLESS` | `true` | Run Chromium headless |
| `PLAYWRIGHT_SLOWMO` | `50` | Slow-mo ms for Playwright |
| `ADZUNA_APP_ID` | _(empty)_ | Optional Adzuna app ID for India internship search |
| `ADZUNA_APP_KEY` | _(empty)_ | Optional Adzuna app key; keep server-side |

---

##  Security Design

- Synthetic demo origins remain local-only. Application origins require separate, exact HTTPS approval and public DNS validation.
- Unconfirmed facts are **never** auto-shared with any portal.
- Ambiguous fields (e.g. generic "Location") are always held for user review.
- LLM output is validated against a strict canonical field allowlist — hallucinated fields are rejected.
- The final submission button is **never** clicked automatically.
- External application fields receive Passport data only after fact-level sharing permission and explicit mapping and preparation approval.

---

## Architecture Highlights

| Component | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI, SQLite (WAL mode) |
| Frontend | Vanilla HTML/CSS/JavaScript (no framework) |
| AI Field Translator | Rule-based mock + LLM (Gemini/OpenAI/Anthropic) |
| Browser Automation | Playwright (async, Chromium) |
| Data Model | Versioned facts with provenance, semantic types, permissions |
| Test Suite | pytest (unit, integration, policy, and synthetic portal fixture coverage) |

See `docs/ARCHITECTURE.md` for the full design.

---

##  License

MIT — Hackathon MVP. Synthetic data only.
# Persistent workflows

Phase 6 adds backend-persisted career workflow goals, checkpoints, activity history, pause/resume/cancel, and user decision endpoints around the existing fixed tool registry. See [docs/WORKFLOWS.md](docs/WORKFLOWS.md) for the state model, APIs, recovery behavior, and current limits.

## Phase 7 hardening

Phase 7 adds workflow session scoping, stale-decision rejection, credential redaction in workflow text, saved-listing revalidation before preparation recovery/fill, and stronger browser request restrictions. See [docs/PHASE_7_HARDENING.md](docs/PHASE_7_HARDENING.md) for the audit, tests, results, and known limitations.
