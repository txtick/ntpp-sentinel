# Sentinel — AI Context

## Project Overview

Sentinel is an automation/orchestration service for NTX Pool Pros.

It:
- Receives webhooks from GoHighLevel (GHL)
- Tracks missed calls and overdue SMS responses
- Stores issues in SQLite
- Sends summary notifications to managers
- Accepts SMS-based manager commands to manage issues

Stack:
- FastAPI
- SQLite
- GHL REST API
- Docker (deployed on DigitalOcean droplet)
- Reverse proxied via Caddy

---

## Deployment Model

Local development:
- Edit locally in VS Code / Cursor
- Push to GitHub
- SSH to droplet
- `git pull`
- `docker compose build --no-cache`
- `docker compose up -d`

Production:
- Runs in Docker container
- Uses environment variables
- GHL API calls MUST include:
  - Authorization Bearer token
  - Version header
  - LocationId header

---

## Core Concepts

### Issue

Each issue represents:
- A missed call
- An overdue SMS
- A follow-up item requiring manager action

Stored in `issues` table.

Status values:
- OPEN
- RESOLVED
- SPAM

Issues are ordered by `due_ts ASC`.

---

## Summary Behavior

Morning / periodic summary:
- Groups issues into:
  - Calls
  - Texts
- Displays:
  - #ID
  - Name or masked phone
  - Last inbound time
  - Due time
  - inbound_count (for SMS only)
- Includes a command footer.

Formatting consistency is critical.
All list-style outputs should match summary formatting.

---

## Manager Command Interface

Managers interact via SMS in their GHL conversation thread.

Rules:

- Commands are case-insensitive.
- "Sentinel" keyword is NOT required.
- `#` prefix is NOT required.
- Multiple IDs supported (e.g., `resolve 3 5 6` or `resolve 3,5,6`).

Supported commands:

- List
- More
- Open <id>
- Resolve <id(s)>
- Spam <id(s)>
- Note <id> <text>

---

## LIST / MORE Behavior

- `list` returns top 5 OPEN issues.
- `more` returns next 5.
- Results formatted like summary.
- Must stay under SMS limits.
- If no more results:
  - Respond: "No more OPEN issues. Reply: List"

Pagination state is in-memory per manager contact.

---

## SMS Constraints

- GHL SMS limit ~1600 characters.
- All manager replies must be chunked if necessary.
- Avoid verbose debug-style output.
- Keep formatting compact and operational.

---

## GHL API Rules

All GHL API calls must include:
- Authorization
- Version
- LocationId

Conversation flow:
- Managers are identified by contactId.
- ConversationId is dynamically resolved.
- Messages are sent via /conversations/messages.

---

## Design Principles

- Minimize manager friction.
- Reduce required keystrokes.
- Maintain consistent formatting.
- Avoid duplication between summary and list formatting.
- Fail safely (never crash on manager SMS).
- Keep commands predictable and simple.

---

## Future Improvements (Planned)

- Shared formatting function for summary + list.
- Persistent paging state (optional).
- Per-manager daily digest metrics.
- Rate limiting for inbound commands.
- Better logging around send failures.

---

## Non-Goals

- Sentinel does not replace GHL.
- Sentinel does not modify pipelines.
- Sentinel does not auto-resolve without manager instruction.
- Sentinel is not a CRM.

---

## Operational Philosophy

Sentinel exists to:
- Improve follow-up accountability
- Prevent missed revenue
- Reduce cognitive load for managers
- Make SMS-based operations fast and frictionless