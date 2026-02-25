# Sentinel – Master State

Project: Sentinel (NTX Pool Pros)  
Status: Active Development (Operational in Production)  
Last Updated: 2026-02-25  
Version: 0.1.0  

---

## 1. Purpose

Sentinel is an internal automation/orchestration service for NTX Pool Pros.

It ingests operational events (primarily GoHighLevel webhooks) and produces deterministic issue tracking and scheduled manager rollups.

Sentinel is designed around:

- Low-noise signal
- Deterministic issue lifecycle
- Scheduled rollups (not real-time alert spam)
- Operational reliability over clever automation

---

## 2. Architecture

### Runtime
- Python 3.11
- FastAPI + Uvicorn
- Docker Compose
- Caddy reverse proxy
- SQLite persistence

### Deployment
- Host: Linux VPS ("droplet")
- Public URL: `https://sentinel.northtexaspoolpros.com`
- Reverse proxy: Caddy → `sentinel:8000`
- Timezone: America/Chicago (container TZ set)

### Volumes
- `./data` → `/data` (SQLite DB)
- `./logs` → `/logs` (cron + runtime logs)

### Container Behavior
- Cron installed inside container
- Uvicorn runs as primary process
- Cron schedules jobs (resolver, summaries, escalations)

---

## 3. Integrations

### GoHighLevel (LeadConnectorHQ)

Base URL: https://services.leadconnectorhq.com


Auth:
- Bearer Private Integration token
- Required `Version: 2021-07-28` header

#### Inbound
- `/webhook/ghl/inbound_sms`
- `/webhook/ghl/unanswered_call`
- `/webhook/ghl` (raw logger)

#### Outbound (Confirmed Contract)
POST /conversations/messages
{
"type": "SMS",
"message": "<text>",
"conversationId": "<id>",
"contactId": "<id>"
}


Important:
- `type` must be string `"SMS"`
- `message` is required content key
- Missing `Version` header causes 401
- Invalid type or missing message causes 422

#### Conversation Lookup

GET /conversations/search?contactId=...

Sentinel stores `conversation_id` on the issue.

---

## 4. Data Model (SQLite)

Database: `/data/sentinel.db`

### raw_events
Append-only webhook payload storage.

### issues

Fields:
- id
- issue_type (`SMS` | `CALL`)
- contact_id
- phone
- conversation_id
- created_ts
- due_ts
- status (`OPEN` | `RESOLVED` | `SPAM`)
- resolved_ts
- first_inbound_ts
- last_inbound_ts
- inbound_count
- outbound_count
- meta (JSON)

### spam_phones
Manual suppression list.

### kv_store
Key-value store used for:
- `last_summary_ts_{slot}`

---

## 5. Core Business Logic

### SMS Issue Lifecycle (Locked)

When inbound customer SMS webhook fires:

1. If no OPEN SMS issue for conversation:
   - Create issue
   - Set:
     - first_inbound_ts
     - last_inbound_ts
     - inbound_count = 1
     - due_ts = +2 business hours
   - DO NOT reset due_ts on later inbound messages

2. If issue exists:
   - Update last_inbound_ts
   - inbound_count += 1
   - DO NOT reset due_ts

Clock always starts at first inbound.

---

### Resolver Logic

Runs:
- Every 15 minutes (business hours)
- Also immediately before summary

For each OPEN SMS issue:

Fetch messages via: GET /conversations/{conversationId}/messages

If ANY outbound message exists where:

direction == "outbound"
AND dateAdded > first_inbound_ts


→ Mark issue RESOLVED.

Outbound resolves issue permanently.

New inbound after resolution starts a new issue.

---

### CALL Issue Logic (Deterministic)

Only created when webhook contains:

voicemail_route = tech_sentinel


No inference from:
- recordings
- duration
- generic inbound calls

CALL issues use same 2 business hour SLA.

---

## 6. SLA Rules

### Default SLA
2 business hours

Business window:
Mon–Fri  
09:00–18:00 America/Chicago

Business hours adder is deterministic and window-aware.

---

## 7. Escalation Logic

Escalated if:

OPEN for ≥ 24 business hours  
(from first_inbound_ts for SMS, created_ts for CALL)

Escalated items appear in summary under:

⚠️ Escalated (24+ business hrs)

---

## 8. Scheduled Jobs

### poll_resolver
- Every 15 minutes
- Business hours only

### send_summary
- 08:00 Mon–Fri (Morning)
- 11:00 Mon–Fri (Midday)
- 15:00 Mon–Fri (Afternoon)

Runs resolver immediately before generating summary.

### escalations
Hourly 09:00–17:00 (currently placeholder endpoint)

---

## 9. Manager Summary Structure (v1)

Sections:

- Missed / Unanswered Calls
- Unanswered Customer Texts
- ⚠️ Escalated (24+ business hrs)
- ✅ Resolved since last summary

Resolved section:
- Shows only once per slot
- Controlled via kv_store timestamp
- Disappears after next summary

Manager-only distribution.

Tech-specific summaries deferred.

---

## 10. Internal Commands

Sentinel listens for:

SENTINEL LIST
SENTINEL SPAM <phone>
SENTINEL RESOLVE <phone|contact_id|name>


Non-command internal texts are ignored.

---

## 11. Design Philosophy

Sentinel is intentionally:

- Deterministic
- Low-noise
- Polling-based (not webhook-reliant for resolution)
- Resistant to duplicate webhooks
- Conversation-centric (not message-centric)

No real-time alert spam.
Only scheduled rollups.

---

## 12. Known Constraints

- Shared GHL inbox prevents tech-specific routing (v1 limitation)
- Conversation search API response shapes vary
- GHL rate limits not formally characterized
- No external monitoring yet
- No idempotency keys at raw_event level (issue-level dedupe only)

---

## 13. Backlog (Next Phase)

- Admin debug endpoint
- Skimmer integration
- Idempotency strategy
- Alerting/monitoring
- Rate limit tracking
- Structured logging
- Repo CI
- Automated deploy

---

## 14. Change Log

### v0.1.0 (2026-02-25)

- Locked SMS issue lifecycle rules
- Implemented business-hour SLA engine
- Implemented polling resolver
- Implemented manager-only summaries
- Added escalation logic
- Dockerized deployment with cron
- Confirmed GHL SMS send contract


