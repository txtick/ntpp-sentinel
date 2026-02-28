## Sentinel Operations Runbook

NTX Pool Pros - Production

- Server: `sentinel`
- App path: `/opt/ntpp-sentinel`
- URL: `https://sentinel.northtexaspoolpros.com`

This is the single deploy, startup, recovery, and verification guide.

---

## 1. Normal Deploy

Preferred (from server):

```bash
cd /opt/ntpp-sentinel
./deploy.sh
```

From laptop:

```bash
ssh kevin@sentinel '
  cd /opt/ntpp-sentinel &&
  ./deploy.sh
'
```

Quick verify:

```bash
ssh kevin@sentinel '
  cd /opt/ntpp-sentinel &&
  docker compose ps &&
  docker compose logs -n 50 sentinel
'
curl -s https://sentinel.northtexaspoolpros.com/health
```

Expected health:

```json
{"ok": true}
```

---

## 2. Server Verification Commands

Use these to check queue health and current state.

Saved trace script (recommended):

```bash
cd /opt/ntpp-sentinel
./trace.sh +12146323629
```

Optional overrides:

```bash
PHONE=+12146323629 BASE=https://sentinel.northtexaspoolpros.com LOG_TAIL=800 ./trace.sh
```

All active queue items:

```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT id, issue_type, status, COALESCE(contact_name,'(no name)') AS name, phone, created_ts, due_ts
FROM issues
WHERE status IN ('PENDING','OPEN')
ORDER BY due_ts ASC;
"
```

Counts by status:

```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT status, COUNT(*) AS count
FROM issues
GROUP BY status
ORDER BY status;
"
```

Recent issue activity:

```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT id, issue_type, status, COALESCE(contact_name,'(no name)') AS name, phone, created_ts, due_ts, resolved_ts
FROM issues
ORDER BY id DESC
LIMIT 25;
"
```

Inspect a specific issue:

```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT id, issue_type, status, contact_name, phone, contact_id, conversation_id, created_ts, due_ts, resolved_ts, meta
FROM issues
WHERE id=42;
"
```

Recent webhook events for a phone:

```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT id, received_ts, source
FROM raw_events
WHERE payload LIKE '%+19403899207%'
ORDER BY id DESC
LIMIT 20;
"
```

Run jobs manually:

```bash
curl -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/verify_pending" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"

curl -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/poll_resolver" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"

curl -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/escalations" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Summary test:

```bash
curl -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

---

## 3. Environment Verification

Check important env values inside container:

```bash
docker exec -it ntpp-sentinel sh -lc 'echo "$WEBHOOK_SECRET" | wc -c'
docker exec -it ntpp-sentinel sh -lc 'echo "$GHL_TOKEN" | wc -c'
docker exec -it ntpp-sentinel sh -lc 'echo "$GHL_LOCATION_ID"'
docker exec -it ntpp-sentinel sh -lc 'echo "$MANAGER_CONTACT_IDS"'
docker exec -it ntpp-sentinel sh -lc 'echo "$INTERNAL_CONTACT_IDS"'
docker exec -it ntpp-sentinel sh -lc 'echo "$INTERNAL_USER_IDS"'
```

If missing, update `/opt/ntpp-sentinel/.env` and redeploy.

---

## 4. Cron Verification

Cron is generated from `.env` at startup.

Active runtime cron:

```bash
docker exec -it ntpp-sentinel sh -lc 'crontab -l'
```

Cron logs:

```bash
tail -f /opt/ntpp-sentinel/logs/cron.log
```

Key cron env variables:

- `CRON_DOW`
- `CRON_MORNING_HOUR`
- `CRON_MIDDAY_HOUR`
- `CRON_AFTERNOON_HOUR`
- `CRON_BUSINESS_HOURS`
- `CRON_BUSINESS_END_HOUR`
- `CRON_ESCALATIONS_EVERY_MINUTES`
- `CRON_POLL_RESOLVER_EVERY_MINUTES`
- `CRON_VERIFY_PENDING_EVERY_MINUTES`

---

## 5. Troubleshooting

Container not healthy:

```bash
cd /opt/ntpp-sentinel
docker compose logs -n 200 sentinel
```

Webhook/job 500s:

```bash
docker compose logs -n 300 sentinel | rg "Traceback|ERROR|verify_pending|poll_resolver|escalations"
```

If you see missing env errors, fix `.env` then:

```bash
cd /opt/ntpp-sentinel
docker compose up -d --build --remove-orphans
```

---

## 6. Rollback

List commits:

```bash
cd /opt/ntpp-sentinel
git log --oneline --decorate -n 20
```

Checkout previous known-good commit and restart:

```bash
cd /opt/ntpp-sentinel
git checkout <commit_hash>
docker compose up -d --build
```

Return to `main` after incident:

```bash
cd /opt/ntpp-sentinel
git checkout main
git pull --ff-only
docker compose up -d --build
```

---

## 7. Confidence Checklist

Before done:

- `/health` returns `{"ok": true}`
- `verify_pending` manual call returns `200`
- `poll_resolver` manual call returns `200`
- `escalations` manual call returns `200`
- `crontab -l` matches expected schedule
- `cron.log` shows successful job calls

Run these checks:

```bash
# 1) Health
curl -i -s https://sentinel.northtexaspoolpros.com/health

# 2) Jobs (expect HTTP/1.1 200 and JSON body)
curl -i -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/verify_pending" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"

curl -i -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/poll_resolver" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"

curl -i -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/escalations" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"

# 3) Summary dry run
curl -i -s -X POST "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"

# 4) Runtime cron + cron activity
docker exec -it ntpp-sentinel sh -lc 'crontab -l'
tail -n 50 /opt/ntpp-sentinel/logs/cron.log
```
