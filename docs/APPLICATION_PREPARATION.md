# Universal application preparation

## Architecture audit

Phase 5 extends the existing workflow rather than adding a second browser system. `PlaywrightManager` still owns the Chromium process, `FormAutomator` inspects and fills controls, `FieldTranslator` proposes canonical meanings, Passport facts retain their version and sharing metadata, saved opportunities supply the stored listing URL, and the existing `receipts` table and Handoffs view receive a summary receipt. New preparation-specific state is isolated in `backend/app/preparation/` and persisted alongside those existing records. The synthetic Northstar and Summit workflow keeps its existing local-origin and API paths.

## User flow

1. In Saved opportunities, choose **Prepare application**. CarryFlow previews the stored listing destination and removes known credential-like query parameters.
2. Confirm the destination in the UI. If needed, the exact HTTPS origin is added to the persistent approved-origin list. This approval permits the browser to open that website; it does not permit sharing any Passport fact.
3. CarryFlow opens the saved URL in the existing Playwright browser and inspects visible form metadata. Password, OTP, MFA, CAPTCHA, payment, hidden, and submit controls are omitted. File controls are shown as unsupported. Radio and checkbox groups are visible but not automatically filled.
4. The existing translator proposes a canonical Passport match. A proposal is only shown as eligible when the fact is user verified and explicitly permitted for the exact destination origin. The user can approve, remap to an existing compatible fact, reject, or leave a field blank. If an accepted fact lacks destination permission, its field shows a direct link to the existing Passport sharing controls, prefilled with that exact destination; the user must still save that sharing choice and reinspect the page.
5. The user approves the reviewed plan and separately starts filling. The backend rechecks the origin, field signature, visibility, fact version/status, and exact-origin permission. The browser fills approved text-like controls and never clicks Submit.
6. A receipt records field labels and counts, skips, permission blocks, failures, recovery state, and the approval reference. It contains no Passport values or browser credentials. The prepared page remains in the Playwright browser for review.

## Destination and browser security

The saved listing URL is untrusted. Only HTTPS on port 443 is accepted. URL credentials, local hostnames, IP literals outside the public range, private/reserved/link-local DNS results, malformed URLs, unsupported schemes, and unusual ports are rejected. DNS results must all be globally routable. The validated address is pinned into Chromium's resolver rules for the approved host, and each request is checked again. The browser context is restricted to the one approved origin for the active navigation; cross-origin requests are blocked. Only GET and HEAD requests are allowed, top-level navigation is only allowed for the requested URL and its redirect chain, service workers are disabled, WebSocket connections are closed, and the initial script blocks form submission, fetch, XHR, beacons, EventSource, and peer-to-peer WebRTC APIs. Immediately before CarryFlow writes an approved value into a field, it locks all page-initiated network requests for that browser session. Navigation and inspection have bounded timeouts.

Redirects are allowed only within the approved origin. A redirect to a different origin is blocked and requires a new explicit origin review. The current UI does not automatically follow or adopt a cross-origin redirect. A removed saved opportunity or a changed listing destination blocks fill and recovery until the user reviews the current listing again. The Passport permission editor accepts exact public HTTPS origins, separately from browser origin approval. Changing a fact's permissions is a distinct auditable user action. The LLM can influence only a proposed canonical mapping; the API allowlist, active Passport fact, exact permission, field signature, and explicit user review remain authoritative.

Website labels and descriptions are treated as untrusted data, bounded before mapping, escaped in the UI, and never interpreted as instructions. The flow does not inspect full page text, read credential fields, click controls, handle authentication, or store field values in receipts.

## API

- `GET /api/preparations/preview/{opportunity_id}` — validate and preview the saved listing destination.
- `GET /api/preparations/origins` — list explicitly approved application origins.
- `POST /api/preparations/origins/approve` — add one exact public HTTPS origin after user confirmation.
- `POST /api/preparations/start` — open a saved listing on an approved origin and inspect safe fields.
- `GET /api/preparations` and `GET /api/preparations/{preparation_id}` — retrieve preparation state.
- `PUT /api/preparations/{preparation_id}/fields/{field_id}/review` — approve, map, reject, or leave a field blank.
- `POST /api/preparations/{preparation_id}/approve` — approve the reviewed plan.
- `POST /api/preparations/{preparation_id}/fill` — revalidate and fill approved fields only.
- `GET /api/preparations/{preparation_id}/receipt` — retrieve the handoff receipt.
- `POST /api/preparations/{preparation_id}/recover` — reopen the validated page and re-inspect; prior mapping approvals are cleared and filling is not resumed.

Chat can preview a saved destination and read the latest preparation receipt (field names and outcome counts only). It can also guide the user to field review controls. Chat cannot open the site, approve origins or mappings, grant Passport permissions, or fill fields. Continue the explicit review flow in Saved opportunities.

## Limits and Phase 6 direction

Resume-file selection/upload is intentionally unsupported. The original Phase 4 resume is not retained, and there is no reviewed file-transfer control yet. Sites that require login, OTP/MFA, CAPTCHA, cross-origin scripts/assets, browser API writes, or a cross-origin redirect may not render or be usable; CarryFlow will leave those controls untouched. Public websites are not used in tests. The system is for one trusted local user and still needs application-level authentication/CSRF protection before network exposure.

Synthetic HTML fixtures now cover simple, ambiguous, required/optional, dynamic, submission-attempt, redirect, instruction-like, and unsupported-field pages. Automated browser interaction remains conditional on Chromium being available; deterministic request-guard tests run without it. Keep submit actions outside automation.
