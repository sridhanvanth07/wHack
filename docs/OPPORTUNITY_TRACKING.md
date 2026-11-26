# Opportunity Management and Tracking

## Saved listing records

CarryFlow stores a compact JSON snapshot of each normalized discovery result alongside a stable local record ID, a hash of its normalized HTTPS listing URL, status, saved/updated timestamps, and first reported application date. The snapshot retains source URLs/attribution, role, company, location, compensation, deadline, skills, description, and retrieval time. A unique identity hash makes repeat saves idempotent; tracking data remains local in the existing SQLite database.

Removing a listing hides it from the saved list and clears its notes. Its status history remains available for audit. Saving that listing again reactivates its existing record. Repeated status updates do not create duplicate history entries. Entering `APPLIED` records the first user-reported application date; CarryFlow does not infer that the user applied.

## Recent search references

The browser creates a random session ID and sends it with chat requests. After each discovery search, the backend replaces that session’s recent result references with the results it actually returned. References expire after two hours. Chat requests such as “save the first one” resolve against this backend-held context; opportunity IDs supplied to the save API must match the session’s recent results. The context is a small lookup for the current local chat session, not a general conversation memory feature.

This app has no user accounts or multi-user isolation. The session ID prevents accidental cross-chat references in the local browser; it is not an authentication credential.

## Status and deadlines

Supported statuses are `SAVED`, `PREPARING`, `APPLIED`, `INTERVIEW`, `OFFER`, `REJECTED`, `WITHDRAWN`, and `EXPIRED`. Status updates may move directly between any values. Each actual change records its previous/new value and timestamp. All changes originate from explicit user instructions or dashboard controls.

Deadline sorting puts known source-provided deadlines first, earliest first, and leaves unknown deadlines last. A missing deadline is shown as “Deadline not provided”; it does not imply that a listing remains open. CarryFlow does not calculate estimated deadlines.

## Private notes and endpoints

Notes are plain text stored locally and escaped by the UI. The Saved tab supports status filtering, deadline sorting, status updates, unsaving, and note add/edit/delete. Chat also supports saving current results, listing/filtering saved records, removing a selected record, updating status, viewing status history, and adding a note.

The API is under `/api/opportunities`: `POST /save`, `GET /saved`, `GET /saved/{id}`, `DELETE /saved/{id}`, `PUT /saved/{id}/status`, `GET /history`, and note `POST`/`PUT`/`DELETE` routes under `/saved/{id}/notes`.

No opportunity-management action navigates to a source, fills a form, or submits an application. The existing synthetic browser workflow remains separate from opportunity tracking.
