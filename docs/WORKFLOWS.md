# Persistent career workflows

CarryFlow now wraps the existing discovery, opportunity, Passport, resume, preparation, browser and receipt services in a small SQLite backed orchestration layer. It does not replace those services or grant the chat model direct database or browser access.

## Architecture and planning

`backend/app/workflows/service.py` owns workflow creation, the allowed state transition map, activity, checkpoints, pending user decisions and compact structured memory. Chat still parses deterministic intents and executes only names in `TOOL_REGISTRY`; every tool input goes through the existing Pydantic and policy checks. A bounded plan is created with a goal and updated only from successful tool results. Discovery results, saved opportunity IDs, preparation IDs and query criteria are references or summaries; Passport values, uploaded files, credentials, cookies and page contents are not copied into workflow memory.

Each successful operation records an activity row and a deduplicated checkpoint. Failures enter `PARTIALLY_COMPLETED` and leave a recovery note directing the service to inspect persisted records before a retry. Search results and previews pause at `WAITING_FOR_USER`. Existing Phase 4 and Phase 5 confirmation and origin, mapping, and fill approval gates continue to be enforced by their services. Application submission remains unavailable.

## Workflow API

- `POST /api/workflows` creates a workflow with `{goal, session_id}`.
- `GET /api/workflows?session_id=...` lists recent workflows. Detail, history, activity, checkpoints, next-step, pending approvals, transitions and decision endpoints require the same `session_id` used when creating the workflow. Unknown or cross-session workflow IDs return not found.
- `POST /api/workflows/{id}/pause`, `/resume`, `/cancel`, and `/complete` use server validated state transitions.
- `POST /api/workflows/{id}/decisions` records a user checkpoint; `/decisions/{decision_id}/respond` stores the response.
- `GET /api/workflows/{id}/history`, `/activity`, `/checkpoints`, and `/next-step` expose progress and the suggested next action.
- `GET /api/workflow-approvals/pending` lists workflows waiting for a separate approval gate.

The chat supports pause, resume and cancel for the workflow in that chat session. Goal and checkpoint state is backend persisted and survives reloads. The chat transcript itself is not persisted; the frontend remains a presentation layer, not the source of truth. Credential-like values in goal and decision text are redacted before retention. Session IDs provide accidental cross-chat isolation for this single-user local prototype; they are not authentication credentials.

## Current limits and next phase

Planning is deterministic and intentionally shallow: it routes existing supported requests and asks the user to choose after a search. It does not automatically save a ranked batch or prepare applications. Workflow decisions are session scoped and single use; unresolved decisions are closed when a workflow is resumed, completed or cancelled. A broader LLM planner should only be considered after tool schemas and policy boundaries are stable, with each proposed action independently validated and approved by deterministic services.
