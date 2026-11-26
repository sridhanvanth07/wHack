# Security model

## Enforced in the demo

- Synthetic demo pages stay within the local allowlist. External application pages require separate, persistent, exact-origin approval, HTTPS, public DNS, and a DNS-pinned Chromium navigation.
- The external browser session is restricted to one approved origin. Cross-origin requests, non-read HTTP methods, page-initiated top-level navigation, and form submission are blocked.
- Chromium service workers are blocked, WebSocket connections are closed, and page-side WebRTC peer connections are disabled in addition to fetch, XHR, beacon, and EventSource restrictions.
- Page content is untrusted data and has no path to alter the user goal, permissions, or execution policy.
- Workflow endpoints and dashboard state require the workflow's chat session ID; the ID provides accidental isolation only and is not authentication.
- Credential-like strings in workflow goals and decisions are redacted before persistence. This is a best-effort filter, not a general secret detector.
- Fact permissions are checked server-side before filling.
- Unresolved conflicts and ambiguous fields are withheld.
- Final submission is separate from preparation and requires an explicit approval action.
- Duplicate action keys are persistent; verified, prepared, and uncertain actions are not blindly replayed.
- Passport facts are sent to an external application only after an explicit field mapping review, a separate exact-origin Passport permission, and preparation approval. The browser blocks form submission.
- Synthetic student data only; no passwords, OTPs, or payment credentials are stored.

## Limitations and production controls

The current UI and API are intended for a single local demo user, not deployment on a public network. Add authentication, CSRF protection, secure origin binding, rate limiting, database migrations, secrets management, encrypted sensitive values, audit retention controls, and a sandboxed Playwright worker before production. A malicious site can still exploit browser or model weaknesses; allowlisting, DNS pinning, restricted actions, and approval reduce scope but cannot eliminate risk. External sites that require authentication, cross-origin assets, scripts, or writes may not work. Cross-origin redirects, file-upload controls, CAPTCHA, OTP, MFA, and real application submission remain unsupported.
