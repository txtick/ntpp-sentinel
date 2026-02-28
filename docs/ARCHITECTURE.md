# Sentinel Architecture

Sentinel is an operations automation service on top of GoHighLevel (GHL) for missed-call/text follow-up control.

Reference state doc:
- `docs/SENTINEL_MASTER_STATE.md` is the canonical source for current runtime behavior.

## 1) System Overview

External system:
- GoHighLevel (GHL)

Sentinel responsibilities:
- Ingest webhook events
- Create/update issue records
- Apply business-hour SLA timing
- Detect valid response activity
- Send manager alerts/summaries

## 2) Runtime Components

- FastAPI app (`ntpp-sentinel`)
- SQLite database (`/data/sentinel.db`)
- Caddy reverse proxy (`ntpp-caddy`)
- In-container cron (schedule generated from `.env` at startup)

## 3) Core Event Flow

Inbound SMS:
- `POST /webhook/ghl/inbound_sms`
- Creates/updates `PENDING` SMS issues
- Suppresses known false-positive patterns (internal-thread + ack closeout)

Missed/unanswered call:
- `POST /webhook/ghl/unanswered_call`
- Controlled by `voicemail_route=tech_sentinel`
- Creates `PENDING` CALL issues

Verification/resolution:
- `POST /jobs/poll_resolver`
- `POST /jobs/verify_pending`
- Detects employee outbound and promotes/resolves issues

Notifications:
- `POST /jobs/escalations` (one-time breach alerts)
- `POST /jobs/send_summary` (manager rollups)

## 4) Data Model (Key Tables)

`issues`:
- `id`, `issue_type`, `status`
- `contact_id`, `phone`, `conversation_id`
- `created_ts`, `due_ts`, `resolved_ts`
- `first_inbound_ts`, `last_inbound_ts`
- `inbound_count`, `outbound_count`
- `breach_notified_ts`
- `meta`

`raw_events`:
- Inbound payload archive for debugging/audit

`conversation_state`:
- Internal outbound markers used by suppression logic

`kv_store`:
- Summary watermarks (`last_summary_ts`, etc.)

`conversation_ai_gate`:
- Optional AI gate cache by conversation watermark

## 5) Design Principles

- Deterministic first
- Fail-open for AI gate (do not suppress on uncertainty)
- Idempotent/retry-safe jobs
- Low-noise operator UX
- Operational observability via concise flow logs
