# Sentinel Architecture

Sentinel is a deterministic automation layer built on top of GoHighLevel.

---

# 1) System Overview

External System:
    GoHighLevel (GHL)

Sentinel Responsibilities:
    - Ingest events
    - Track issues
    - Enforce SLA timing
    - Detect resolution
    - Notify managers

---

# 2) Event Flow

Customer SMS → GHL → Webhook → Sentinel

Sentinel:
    - Creates issue
    - Assigns due_ts
    - Stores event

Manager reply → GHL
Sentinel poll_resolver:
    - Detects outbound reply
    - Marks issue RESOLVED

Scheduled job:
    - send_summary
    - Sends manager summary SMS

---

# 3) Core Tables

## issues

Tracks open and resolved issues.

Fields:
- id
- type (SMS / CALL)
- contact_id
- conversation_id
- state (OPEN / RESOLVED)
- created_ts
- due_ts
- resolved_ts

---

## kv_store

Stores watermarks and runtime state.

Examples:
- last_summary_ts_morning
- last_summary_ts_midday
- last_summary_ts_afternoon

---

## raw_events

Stores inbound webhook payloads for debugging.

---

# 4) API Endpoints

Inbound:

    POST /webhook/ghl
    POST /webhook/ghl/unanswered_call

Internal jobs:

    POST /jobs/poll_resolver
    POST /jobs/send_summary

---

# 5) Design Principles

Sentinel must be:

- Deterministic
- Idempotent
- Observable
- Testable via curl
- Safe to re-run

Jobs must not cause duplicate actions when retried.

---

# 6) Failure Modes

If GHL API fails:
    - Log error
    - Do not advance watermark
    - Allow retry

If database write fails:
    - Fail fast
    - Return non-200 response

---

# 7) Future Enhancements

- Business-hour SLA logic
- Per-manager routing
- Voicemail transcript ingestion
- Escalation ladder (tiered)
- Structured logging