# Phase 7 — Integration, Security and Reliability Hardening

## What was audited

Reviewed the FastAPI route layer and frontend request/rendering flow; SQLite initialization and service transactions; deterministic conversation parser, fixed tool registry and executor; discovery adapters and session result references; saved opportunities, notes and status history; Passport facts, provenance and permissions; resume parsing/review; preparation approval, field fingerprints, receipts and recovery; Playwright origin validation, network routing and form filling; workflow state, activity, decisions and checkpoints; and the existing pytest suite and Phase 1–6 documentation.

## Issues discovered and fixes

- Workflow detail, activity, checkpoint, decision, transition and pending-approval endpoints did not check the workflow's stored chat session. Dashboard state could also return workflows from other chat sessions. Endpoints now require the creating `session_id`; dashboard state is filtered to its chat. Cross-session IDs return not found. This is accidental local session isolation, not authentication.
- A pending decision could be answered after a workflow was cancelled or manually resumed, moving the cancelled workflow back to `RUNNING`. Responses now require `WAITING_FOR_USER`; pending decisions are closed when a workflow is resumed, cancelled or completed. Repeated identical pending-decision requests return the existing decision.
- A failed save request with no recent search results could create a misleading partial workflow. Chat now creates a standalone save or preview workflow only after that tool succeeds; discovery workflows are still checkpointed around the search operation.
- Preparation fill and recovery trusted an old saved opportunity snapshot. Both now check that the listing remains saved and its current normalized destination still matches the reviewed destination before proceeding.
- Workflow goals and decisions were stored verbatim. Credential-like values such as passwords, OTPs, bearer tokens, API keys, cookies and JWTs are redacted before persistence. This is a best-effort filter, not a general secret detector.
- The browser context allowed service workers and did not explicitly close WebSocket or block WebRTC APIs. Service workers are now disabled, WebSockets are closed, page-side peer connections are disabled, and all page-initiated requests are locked before approved Passport values are filled; the existing request guard still restricts origin, method, and navigation.
- The existing browser end-to-end test did not honor `CARRYFLOW_PLAYWRIGHT_DISABLED=1` and caused a long-running test invocation in this restricted environment. It now skips when that environment flag is set; a separate optional synthetic fixture browser test uses the same condition.

## Security and integrity coverage

The new deterministic tests cover unknown tool names and extra tool arguments, injection-style user text, browser GET/POST and top-level navigation policy, cross-origin requests and redirects, egress lock before fill, workflow session scoping, stale and repeated decisions, credential redaction, removed opportunities during fill/recovery, and rollback when saving an opportunity succeeds but its initial history write fails. Existing tests cover HTTPS/port/credential restrictions, public DNS and mixed public/private resolution, Passport fact permissions, changed field fingerprints, approval invalidation during recovery, receipt value redaction, resume file validation, and saved opportunity idempotency.

Eight local HTML fixtures cover simple, ambiguous, required/optional, dynamic, submit-attempt, redirect, instruction-like and unsupported-field pages. The browser fixture test performs real local-page inspection and verifies blocked submission when Chromium is available. In this environment it was skipped; request-guard and service-level checks were run without a live browser. No test contacts a public application portal, submits an application, or relies on live discovery results.

## Tests and checks

Baseline before Phase 7 changes: **131 passed, 3 skipped** using `pytest -q --ignore=backend/tests/test_e2e_workflow.py` with `CARRYFLOW_PLAYWRIGHT_DISABLED=1`. A full baseline invocation did not finish because the end-to-end test ignored that flag; it was interrupted before producing a result.

Final full suite: **148 passed, 5 skipped, 0 failed** using `$env:CARRYFLOW_PLAYWRIGHT_DISABLED='1'; .\.venv\Scripts\python.exe -m pytest -q` in PowerShell. The 5 skips are 3 existing browser-dependent tests, the legacy browser E2E test, and the optional synthetic-browser fixture test. No tests were blocked by live-provider availability. Python compilation, `node --check frontend/app.js`, and `git diff --check` also passed.

There is no configured frontend lint or browser smoke-test harness in the repository. JavaScript syntax was checked, but the dashboard was not exercised in a real browser during this run.

## Remaining limitations and manual verification

- The app remains a single-user local prototype. Session IDs are client supplied and are not credentials; do not expose the service beyond localhost. Authentication, CSRF defense, rate limiting, and production database migration tooling are not implemented.
- Credential redaction covers common explicit patterns but cannot guarantee removal of every secret from arbitrary prose. Avoid putting secrets in goals, notes, or decisions.
- Chromium-specific enforcement for service workers, WebSockets, WebRTC, submission blocking, dynamic DOM changes and screenshots was not independently exercised in this environment because browser execution was disabled. Run the optional fixture test on a machine with Chromium installed and unset `CARRYFLOW_PLAYWRIGHT_DISABLED` to perform that manual verification.
- External discovery adapters are tested with deterministic fixtures; live provider availability and current listing quality were not verified here.
- Workflow memory persists goals, selected saved-opportunity IDs, search criteria, preparation IDs and progress summaries. It does not persist the chat transcript or automatically plan/execute multiple tools. Sensitive preparation still uses the existing separate origin, Passport permission and field-mapping approvals. Application submission remains unavailable through browser automation.
- A locally running browser UI smoke check remains manual; the API and frontend changes passed static and backend regression checks.

## Run locally

From the repository root, activate the existing virtual environment and start Uvicorn bound to loopback:

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. If that port is already in use, choose another local port and keep the application bound to `127.0.0.1`. The Phase 7 build launched during this task is available at `http://127.0.0.1:8007`.

Run the regression suite with `$env:CARRYFLOW_PLAYWRIGHT_DISABLED='1'` only when browser tests cannot run. On a browser-enabled machine, run `pytest -q backend/tests/test_phase7_hardening.py::test_synthetic_portal_fixtures_inspect_safe_controls_without_submission` with that variable unset to exercise the synthetic pages in Chromium; the legacy browser E2E test is separate.

## Phase 8 recommendation

Add a supported frontend smoke-test harness and run the synthetic portals against real Chromium in a controlled test job. Then add request-level idempotency keys for chat mutations and explicit recovery reconciliation that compares the latest checkpoint with current database records. Keep the existing user approval boundaries and localhost-only deployment model.
