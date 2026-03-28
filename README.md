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

Optional Skimmer import values:

```env
SKIMMER_DOWNLOAD_DIR=/data/skimmer
SKIMMER_DB_PATH=/data/skimmer/skimmer.db
SKIMMER_LINK_FILE=/data/skimmer/skimmer_link.txt
# Optional: SKIMMER_ARCHIVE_DIR=/data/skimmer/archive
# Optional: SKIMMER_KEEP_DAILY=1
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

Resolves active SMS and CALL issues when a valid staff follow-up is detected.

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/poll_resolver \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Runs automatically every 15 minutes during business hours on configured cron days.

---

### Send Summary

Manager rollups at 8:00, 11:00, 15:00 on configured cron days.

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

One-time breach alert endpoint during configured business hours:

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/escalations \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

---

### Skimmer Import

Temporary Skimmer export URLs can be posted directly to Sentinel:

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/skimmer_link \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>" \
  -H "Content-Type: application/json" \
  -d '{"skimmer_url":"<temporary_signed_url>"}'
```

Sentinel immediately downloads and decompresses the `.db.gz` export to `SKIMMER_DB_PATH` when a fresh signed URL is provided.

---

## Webhooks

### Inbound SMS

POST /webhook/ghl/inbound_sms

Behavior:
- Creates or updates deterministic SMS issue
- Configurable business-hour SLA
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
- Business hours and SLA are configurable (`BUSINESS_HOURS_*`, `SMS_SLA_HOURS`, `CALL_SLA_HOURS`)
- Default repo cron days are Monday-Saturday via `CRON_DOW=1-6`
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
