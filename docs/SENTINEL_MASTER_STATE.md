## Sentinel Master State — NTX Pool Pros (Updated February 2026)

Repo: `https://github.com/txtick/ntpp-sentinel`  
Public URL: `https://sentinel.northtexaspoolpros.com`  
Timezone: `America/Chicago`

### 1. Purpose

Sentinel ingests GoHighLevel webhooks and maintains deterministic issue tracking for customer communication follow-up.

Core outcomes:
- Tracks `SMS` and `CALL` issues.
- Applies business-hour SLA timing.
- Auto-resolves when valid employee response is detected.
- Sends manager summaries.
- Sends one-time real-time SLA breach alerts.

### 2. Runtime Architecture

- App: FastAPI + Uvicorn in container `ntpp-sentinel`
- Reverse proxy: Caddy in container `ntpp-caddy`
- Data: SQLite at `/data/sentinel.db` (host mount `./data`)
- Logs: `/logs/cron.log` (host mount `./logs`)
- Scheduler: in-container cron, generated from `.env` at startup

### 3. Key Endpoints

Webhooks:
- `POST /webhook/ghl`
- `POST /webhook/ghl/inbound_sms`
- `POST /webhook/ghl/unanswered_call`

Jobs:
- `POST /jobs/poll_resolver`
- `POST /jobs/verify_pending`
- `POST /jobs/send_summary?slot=morning|midday|afternoon&dry_run=0|1`
- `POST /jobs/escalations?dry_run=0|1&limit=200`

Health:
- `GET /health`

Auth for all protected routes:
- Header `X-NTPP-Secret: <WEBHOOK_SECRET>` (or `?secret=` fallback)

### 4. Issue Lifecycle (Current)

Statuses:
- `PENDING`
- `OPEN`
- `RESOLVED`
- `SPAM`

SMS flow:
- Customer inbound generally creates/updates `PENDING`.
- `verify_pending` processes due `PENDING SMS`:
  - Resolves if staff outbound exists after first inbound, or
  - Resolves if ack-closeout is detected after staff reply, or
  - Resolves if optional AI gate confidently says no follow-up needed, or
  - Promotes to `OPEN`.
- If `conversation_id` is missing for due `PENDING SMS`, verifier re-attempts lookup. If still missing, it promotes to `OPEN` (no stuck pending).

CALL flow:
- Controlled signal: `voicemail_route=tech_sentinel`.
- Creates `PENDING CALL` with `due_ts`.
- `verify_pending` resolves if valid staff outbound is detected after creation, else promotes to `OPEN`.

### 5. Auto-Resolve Rules

Staff reply detection is strict:
- Outbound message must include `userId`.
- `userId` must be in `INTERNAL_USER_IDS`.
- Automation messages without `userId` do not resolve issues.

### 6. False-Positive Controls

Internal-thread suppression:
- `INTERNAL_REPLY_GRACE_HOURS` suppresses customer replies after internal-initiated outbound thread activity.

Ack-closeout suppression:
- Detects short acknowledgement/fixed-it style customer replies.
- Includes tapback/reaction prefixes like `liked`, `loved`, `laughed at`, etc.
- Configurable by:
  - `ACK_CLOSE_ENABLED`
  - `ACK_CLOSE_WINDOW_MODE` (`eod` or `hours`)
  - `ACK_CLOSE_WINDOW_HOURS`
  - `ACK_CLOSE_MAX_LEN`

### 7. Optional AI Follow-Up Gate

AI gate is optional and disabled by default (`AI_GATE_ENABLED=0`).

When enabled, for due `PENDING SMS` not already resolved by deterministic rules:
- Classifies whether follow-up is needed.
- Suppresses escalation only for confident `NO` (`AI_GATE_SUPPRESS_NO_CONFIDENCE` threshold).
- Fail-open behavior on errors (defaults to follow-up needed).

Operational safeguards:
- Per-run AI budget and cap:
  - `AI_GATE_MAX_ISSUES_PER_RUN`
  - `AI_GATE_RUN_BUDGET_SECONDS`
- Request timeout:
  - `AI_GATE_TIMEOUT_SECONDS`
- PII redaction in AI transcript:
  - `AI_GATE_REDACT_PII`
- Caches AI decisions by conversation message watermark in `conversation_ai_gate`.
- Writes AI suppression audit fields into issue `meta`.

### 8. Summaries and Escalations

Scheduled summaries:
- Morning, Midday, Afternoon slots.
- Includes overdue calls/texts and "Resolved since last summary".
- Resolved section display capped to 5 items.
- Watermark now uses global key `last_summary_ts` (with slot-key fallback compatibility).

Real-time escalations:
- `jobs/escalations` sends one-time alert for newly breached `OPEN` issues (`due_ts <= now` and `breach_notified_ts IS NULL`).
- Marks `breach_notified_ts` after successful send.
- Breached issues still appear in regular summary until resolved.

### 9. Environment Variables (Current)

Primary:
- `WEBHOOK_SECRET`
- `GHL_TOKEN`
- `GHL_LOCATION_ID`
- `MANAGER_CONTACT_IDS`
- `INTERNAL_CONTACT_IDS`
- `INTERNAL_USER_IDS`

Timing:
- `TIMEZONE`
- `BUSINESS_HOURS_START`
- `BUSINESS_HOURS_END`
- `SMS_SLA_HOURS`
- `CALL_SLA_HOURS`

Behavior:
- `INTERNAL_REPLY_GRACE_HOURS`
- `ACK_CLOSE_*`
- `FLOW_LOG_ENABLED`

AI:
- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `AI_GATE_*`

Cron schedule generation:
- `CRON_DOW`
- `CRON_MORNING_HOUR`
- `CRON_MIDDAY_HOUR`
- `CRON_AFTERNOON_HOUR`
- `CRON_BUSINESS_HOURS`
- `CRON_BUSINESS_END_HOUR`
- `CRON_ESCALATIONS_EVERY_MINUTES`
- `CRON_POLL_RESOLVER_EVERY_MINUTES`
- `CRON_VERIFY_PENDING_EVERY_MINUTES`

### 10. Cron Model (Now Env-Driven)

Static `app/cron/crontab` is no longer authoritative at runtime.

Runtime flow:
- `/app/start.sh` runs on container start.
- `/app/cron/render-crontab.sh` builds `crontab.generated` from env.
- Generated crontab is installed (`crontab -l` is source of truth).

### 11. Logging

Raw event storage:
- Incoming webhook payloads are persisted in `raw_events`.

Operational flow logs:
- `FLOW ...` single-line JSON logs show key transitions (`issue_created`, `promoted_open`, `auto_resolved`, `ignored_*`, `escalations.sent`, `ai_gate.decision`).
- Toggle: `FLOW_LOG_ENABLED`.

### 12. Database Tables (Important)

- `issues` (core state)
- `raw_events` (ingested payloads)
- `spam_phones` (suppression)
- `conversation_state` (internal outbound markers)
- `kv_store` (summary watermarks)
- `conversation_ai_gate` (AI cache)

Notable `issues` fields:
- `status`, `created_ts`, `due_ts`, `resolved_ts`
- `conversation_id`, `first_inbound_ts`, `last_inbound_ts`
- `inbound_count`, `outbound_count`
- `breach_notified_ts`
- `meta` (includes notes and optional AI suppression audit values)

### 13. Practical Verification Commands

Current active crontab:
```bash
docker exec -it ntpp-sentinel sh -lc 'crontab -l'
```

Tail cron job activity:
```bash
tail -f /opt/ntpp-sentinel/logs/cron.log
```

List active workload:
```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT id, issue_type, status, contact_name, phone, due_ts
FROM issues
WHERE status IN ('PENDING','OPEN')
ORDER BY due_ts ASC;
"
```

Force verifier:
```bash
curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/verify_pending" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Dry-run summary:
```bash
curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```
