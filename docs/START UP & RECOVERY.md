Sentinel System Startup & Recovery Guide

NTX Pool Pros – Production

Server: sentinel
Path: /opt/ntpp-sentinel
Public URL: https://sentinel.northtexaspoolpros.com

1. Normal Deploy (Safe Path)

Use this when you've pushed changes to GitHub and want prod updated.

From your laptop:

ssh kevin@sentinel '
cd /opt/ntpp-sentinel &&
git checkout main &&
git pull --ff-only &&
docker compose up -d --build
'

Then verify:

docker compose ps
docker compose logs -n 50 sentinel

Health check:

curl https://sentinel.northtexaspoolpros.com/health

Expected:

{"ok": true}

2. Verify Environment Variables (Critical)

Inside container:

docker exec -it ntpp-sentinel sh -lc 'echo "$WEBHOOK_SECRET"'
docker exec -it ntpp-sentinel sh -lc 'echo "$MANAGER_CONTACT_IDS"'
docker exec -it ntpp-sentinel sh -lc 'echo "$GHL_TOKEN" | wc -c'

If any are blank → fix .env and redeploy.

.env lives at:

/opt/ntpp-sentinel/.env

After editing:

docker compose up -d --build

3. Verify Cron Is Running

Cron log:

tail -f /opt/ntpp-sentinel/logs/cron.log

You should see entries like:

cron: morning -> POST http://localhost:8000/jobs/send_summary?slot=morning

cron: morning <- http=200

If you see:

WEBHOOK_SECRET is not set

→ cron.sh is not loading environment correctly
→ rebuild container

4. Manual Job Testing (Always Test Manually First)

Poll resolver:

curl -X POST https://sentinel.northtexaspoolpros.com/jobs/poll_resolver

-H "X-NTPP-Secret: <WEBHOOK_SECRET>"

Dry-run summary:

curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1
"
-H "X-NTPP-Secret: <WEBHOOK_SECRET>"

Live summary:

curl -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning
"
-H "X-NTPP-Secret: <WEBHOOK_SECRET>"

If manual send works but cron doesn’t → cron issue only.

5. Check Database Directly

Recent issues:

sqlite3 /opt/ntpp-sentinel/data/sentinel.db
"select id, issue_type, status from issues order by id desc limit 20;"

Open issues:

sqlite3 /opt/ntpp-sentinel/data/sentinel.db
"select id, issue_type, phone, due_ts from issues where status='OPEN';"

Watermarks:

sqlite3 /opt/ntpp-sentinel/data/sentinel.db
"select * from kv_store;"

6. If Container Won’t Start

Check logs:

docker compose logs -n 200 sentinel

Common causes:

NameError
Import error
Bad merge
Syntax error

Fix locally → commit → redeploy.

7. If Webhook Returns 500

Check:

docker compose logs -n 200 sentinel

Look for:

Traceback
NameError
conn not defined
Optional not defined

Fix main.py → rebuild container.

8. If GHL Sending Fails

Check logs for:

HTTP 401 → token invalid
HTTP 403 → scope issue
HTTP 404 → wrong endpoint
HTTP 400 → wrong payload format

Correct send contract:

Endpoint:
POST /conversations/messages

Body must include:

"type": "SMS"
"message": "<text>"
"conversationId": "<id>"
"contactId": "<id>"

Header:
Authorization: Bearer <GHL_TOKEN>
Version: 2021-07-28

9. Full Reset (Last Resort)

If system is in a weird state:

cd /opt/ntpp-sentinel
docker compose down
docker compose up -d --build

If DB corruption suspected:

STOP HERE and back up:

cp data/sentinel.db data/sentinel.db.bak

Only wipe DB if absolutely necessary.

10. Safe Rollback

If last deploy broke prod:

cd /opt/ntpp-sentinel
git log --oneline
git checkout <previous_commit_hash>
docker compose up -d --build

To restore main afterward:

git checkout main
git pull

11. Production Confidence Checklist

Before walking away:

• /health returns ok
• Manual poll_resolver works
• Manual send_summary works
• Cron log shows scheduled entries
• Summary SMS received on test

If all five are true → system stable.

12. Mental Model

Sentinel has four pillars:

Webhooks create deterministic issues

Resolver marks them resolved

Summary reports status

Cron orchestrates timing

If something breaks, isolate which pillar failed.