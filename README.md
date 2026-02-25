# Sentinel (NTX Pool Pros)

Sentinel is an internal automation and orchestration service for NTX Pool Pros.

It ingests GoHighLevel (LeadConnectorHQ) webhooks and produces deterministic issue tracking with scheduled manager rollups — intentionally low-noise and operationally reliable.

---

## Architecture

- Python 3.11
- FastAPI + Uvicorn
- Docker Compose
- Caddy reverse proxy
- SQLite persistence
- Cron jobs inside container

### Deployment

- Public URL: https://sentinel.northtexaspoolpros.com
- App container: ntpp-sentinel
- Reverse proxy: Caddy → sentinel:8000
- Timezone: America/Chicago

### Persistent Volumes

- ./data → /data (SQLite DB)
- ./logs → /logs (cron + runtime logs)

---

## Environment Variables

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

Required values:

```env
WEBHOOK_SECRET=<shared_secret_for_jobs_and_webhooks>

GHL_TOKEN=<LeadConnector_private_integration_token>
GHL_VERSION=2021-07-28

MANAGER_CONTACT_IDS=<comma_separated_contact_ids>
```

---

## Running Sentinel

Build and start:

```bash
docker compose up -d --build
```

Restart only:

```bash
docker compose restart sentinel
```

Stop:

```bash
docker compose down
```

---

## Health Check

```bash
curl https://sentinel.northtexaspoolpros.com/health
```

Expected:

```json
{"ok": true}
```

---

## Protected Endpoints

All jobs and webhooks require:

```
X-NTPP-Secret: <WEBHOOK_SECRET>
```

---

## Jobs

### Poll Resolver

Resolves answered SMS issues.

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/poll_resolver \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Runs automatically every 15 minutes during business hours.

---

### Send Summary

Manager rollups at 8:00, 11:00, 15:00 (Mon–Fri).

Dry run:

```bash
curl -X POST \
  "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Live send:

```bash
curl -X POST \
  "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Slots:
- morning
- midday
- afternoon

---

### Escalations

Placeholder endpoint (hourly 9–17 Mon–Fri):

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/escalations \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

---

## Webhooks

### Inbound SMS

POST /webhook/ghl/inbound_sms

Behavior:
- Creates or updates deterministic SMS issue
- 2 business-hour SLA
- Does not reset SLA clock on additional inbound messages

---

### Unanswered Call

POST /webhook/ghl/unanswered_call

Creates CALL issue only when:
voicemail_route = tech_sentinel

---

## SMS Send Contract (Confirmed)

POST /conversations/messages

Payload:

```json
{
  "type": "SMS",
  "message": "<text>",
  "conversationId": "<id>",
  "contactId": "<id>"
}
```

Important:
- `type` must be "SMS"
- `message` is required key
- `Version` header required

---

## Business Logic Summary

- SMS issues created on first inbound
- 2 business-hour SLA (Mon–Fri 9–6)
- Resolver checks for outbound replies
- Outbound resolves issue permanently
- 24 business-hour escalation threshold
- Scheduled manager rollups only (no real-time alerts)

See docs/SENTINEL_MASTER_STATE.md for full authoritative specification.

---

## Logs

View container logs:

```bash
docker logs ntpp-sentinel
```

View cron log:

```bash
tail -f logs/cron.log
```

---

## Versioning

Baseline release: v0.1.0

Future changes should increment semantic version.
