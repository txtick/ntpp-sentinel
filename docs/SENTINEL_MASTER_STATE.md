Sentinel Master State — NTX Pool Pros

Repo: https://github.com/txtick/ntpp-sentinel

Public URL: https://sentinel.northtexaspoolpros.com

Timezone: America/Chicago
Stack: FastAPI + Uvicorn + Docker Compose + Caddy + SQLite + in-container cron

1. Purpose

Sentinel is an automation/orchestration service for NTX Pool Pros.

It ingests GoHighLevel (GHL) webhooks and produces:

• Deterministic CALL + SMS issues
• SLA tracking (business hours aware)
• Automated resolution detection
• Manager summary SMS notifications
• Watermark-based “resolved since last summary” tracking

The system is deterministic by design. No AI logic is required for core issue tracking.

2. Deployment Architecture

Docker Compose runs two services:

sentinel

FastAPI app

Exposes port 8000 internally

Runs cron inside container

caddy

Reverse proxy

Terminates TLS

Routes sentinel.northtexaspoolpros.com → sentinel:8000

Ports 80 and 443 exposed publicly

Volumes:

./data → /data (SQLite database)
./logs → /logs (cron logs)

Database file:
data/sentinel.db

3. Environment Variables (.env)

Required:

WEBHOOK_SECRET=shared_secret_value
GHL_TOKEN=leadconnector_private_token
GHL_VERSION=2021-07-28
MANAGER_CONTACT_IDS=id1,id2,id3

Recommended:

GHL_BASE_URL=https://services.leadconnectorhq.com

TIMEZONE=America/Chicago
DB_PATH=/data/sentinel.db
SUMMARY_MAX_ITEMS_PER_SECTION=8

Important:
MANAGER_CONTACT_IDS must be a comma-separated list of valid GHL contact IDs or summary SMS will not send.

4. Authentication

All webhooks and job endpoints require authentication.

Either:

Header:
X-NTPP-Secret: <WEBHOOK_SECRET>

OR

Query param:
?secret=<WEBHOOK_SECRET>

5. Webhooks
Inbound SMS

POST /webhook/ghl/inbound_sms

Behavior:

• Ignores outbound messages
• If inbound from customer:

Creates OPEN SMS issue if none exists for conversation

SLA = 2 business hours

Stores:
contact_id
phone
conversation_id
first_inbound_ts
last_inbound_ts
inbound_count
meta.contact_name (if available)

Subsequent inbound messages:
update inbound_count + last_inbound_ts
DO NOT reset due_ts

• Internal “SENTINEL ” commands:

OPEN

RESOLVE

SPAM

NOTE

LIST

Unanswered Call

POST /webhook/ghl/unanswered_call

Creates deterministic CALL issue only when voicemail_route=tech_sentinel signal is present.

SLA: 2 business hours.

6. Jobs
poll_resolver

POST /jobs/poll_resolver

Purpose:

• Looks at open issues
• Queries GHL conversation history
• Detects outbound manager replies
• Marks issue RESOLVED
• Sets resolved_ts

Manual trigger example:

curl -X POST https://sentinel.northtexaspoolpros.com/jobs/poll_resolver

-H "X-NTPP-Secret: <WEBHOOK_SECRET>"

send_summary

POST /jobs/send_summary?slot=morning|midday|afternoon&dry_run=0|1

Slots:
morning
midday
afternoon

Dry run (no SMS sent):

curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1
"
-H "X-NTPP-Secret: <WEBHOOK_SECRET>"

Live send:

curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning
"
-H "X-NTPP-Secret: <WEBHOOK_SECRET>"

Summary format includes:

• Overdue Calls count
• Overdue Texts count
• Calls section
• Texts section
• “Resolved since last summary” section
• Reply command footer

Watermark logic ensures resolved items appear only once.

escalations

POST /jobs/escalations

Currently placeholder / future enhancement.

7. Cron

Cron runs inside sentinel container.

Script:
app/cron/cron.sh

Default schedule:

08:00 → morning summary
11:00 → midday summary
15:00 → afternoon summary

poll_resolver runs periodically during business hours.

Cron logs:

Host:
logs/cron.log

View:
tail -f logs/cron.log

Cron loads environment from container process environment, not shell profiles.

8. Database Model

Table: issues

Key fields:

id
issue_type (SMS or CALL)
contact_id
phone
conversation_id
created_ts
due_ts
resolved_ts
status (OPEN, RESOLVED, SPAM)
first_inbound_ts
last_inbound_ts
inbound_count
outbound_count
meta (JSON)

Table: kv_store

Used for watermark tracking per summary slot.

9. Verified GHL Send Contract

Endpoint:

POST /conversations/messages

Payload:

{
"type": "SMS",
"message": "text here",
"conversationId": "conversation_id",
"contactId": "contact_id"
}

Headers:

Authorization: Bearer <GHL_TOKEN>
Version: 2021-07-28

type must be "SMS"
key must be "message"

10. Smoke Test Checklist

Inbound SMS test:

Send text from customer

Verify OPEN issue created in DB

Resolver test:

Manager replies in GHL

Run poll_resolver

Verify issue marked RESOLVED

Summary test:

Run dry_run=1

Confirm formatted output

Run live send

Confirm SMS delivered

Resolve an issue

Run summary again

Confirm appears in “Resolved since last summary” once

11. Common Failure Causes

No summary SMS received:

• MANAGER_CONTACT_IDS empty
• dry_run=1 used
• GHL_TOKEN invalid
• Cron not firing
• WEBHOOK_SECRET mismatch

Inbound SMS failing:

• Missing DB connection in handler
• Invalid auth
• Unexpected payload shape

Cron error “WEBHOOK_SECRET is not set”:

• cron.sh not loading environment
• container rebuilt without env

12. Useful DB Queries

Recent issues:

sqlite3 data/sentinel.db "select id, issue_type, status, phone, contact_id from issues order by id desc limit 20;"

Open issues:

sqlite3 data/sentinel.db "select id, issue_type, phone, due_ts from issues where status='OPEN';"

Watermarks:

sqlite3 data/sentinel.db "select * from kv_store;"

13. Deploy Workflow

Local:

git add -A
git commit -m "message"
git push origin main

Server deploy:

ssh kevin@sentinel '
cd /opt/ntpp-sentinel &&
git checkout main &&
git pull --ff-only &&
docker compose up -d --build
'

14. Current System Status (as of latest session)

• Inbound SMS webhook working
• contact_name stored in meta
• Summary display updated to show names
• Resolver job functioning
• Cron environment loading fixed
• Git workflow corrected
• Live summary send pending verification

15. Next Logical Steps

Confirm manual live summary send works

Confirm cron-delivered summaries arrive on schedule

Add stronger outbound GHL send logging

Optional: backfill contact_name for old issues

Implement escalation policy (e.g., 24 business hours)

Add lightweight admin endpoint for system diagnostics