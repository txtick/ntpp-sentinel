## Sentinel System Startup & Recovery Guide

NTX Pool Pros – Production

- **Server**: `sentinel`
- **Path**: `/opt/ntpp-sentinel`
- **Public URL**: `https://sentinel.northtexaspoolpros.com`

---

## 1. Normal Deploy (Safe Path)

Use this when you've pushed changes to GitHub and want prod updated.

From your laptop:

```bash
ssh kevin@sentinel '
  cd /opt/ntpp-sentinel &&
  git checkout main &&
  git pull --ff-only &&
  docker compose up -d --build
'
```

Then verify:

```bash
docker compose ps
docker compose logs -n 50 sentinel
```

Health check:

```bash
curl https://sentinel.northtexaspoolpros.com/health
```

Expected:

```bash
{"ok": true}
```

---

## 2. Verify Environment Variables (Critical)

Inside container:

```bash
docker exec -it ntpp-sentinel sh -lc 'echo "$WEBHOOK_SECRET"'
docker exec -it ntpp-sentinel sh -lc 'echo "$MANAGER_CONTACT_IDS"'
docker exec -it ntpp-sentinel sh -lc 'echo "$INTERNAL_CONTACT_IDS"'
docker exec -it ntpp-sentinel sh -lc 'echo "$INTERNAL_REPLY_GRACE_HOURS"'
docker exec -it ntpp-sentinel sh -lc 'echo "$GHL_LOCATION_ID"'
docker exec -it ntpp-sentinel sh -lc 'echo "$GHL_TOKEN" | wc -c'
```

If any are blank → fix `.env` and redeploy.

`.env` lives at:

```bash
/opt/ntpp-sentinel/.env
```

After editing:

```bash
docker compose up -d --build
```

---

## 3. Verify Cron Is Running

Cron log:

```bash
tail -f /opt/ntpp-sentinel/logs/cron.log
```

You should see entries like:

```text
cron: morning -> POST http://localhost:8000/jobs/send_summary?slot=morning
cron: morning <- http=200
```

If you see:

```text
WEBHOOK_SECRET is not set
```

- `cron.sh` is not loading environment correctly  
- Rebuild container.

### Cron Schedule Is Now `.env`-Driven

Cron is generated at container startup from `.env` values.

Key variables:

- `CRON_DOW` (default `1-5`)
- `CRON_MORNING_HOUR` (default `8`)
- `CRON_MIDDAY_HOUR` (default `11`)
- `CRON_AFTERNOON_HOUR` (default `15`)
- `CRON_BUSINESS_HOURS` (default `8-16`)
- `CRON_BUSINESS_END_HOUR` (default `17`)
- `CRON_ESCALATIONS_EVERY_MINUTES` (default `1`)
- `CRON_POLL_RESOLVER_EVERY_MINUTES` (default `15`)
- `CRON_VERIFY_PENDING_EVERY_MINUTES` (default `5`)

Verify active schedule inside container:

```bash
docker exec -it ntpp-sentinel sh -lc 'crontab -l'
```

---

## 4. Manual Job Testing (Always Test Manually First)

Poll resolver:

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/poll_resolver \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Dry-run summary:

```bash
curl -X POST \
  "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Live summary:

```bash
curl -X POST \
  "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

If manual send works but cron doesn’t → cron issue only.

---

## 5. Check Database Directly

Recent issues:

```bash
sqlite3 /opt/ntpp-sentinel/data/sentinel.db \
  "select id, issue_type, status from issues order by id desc limit 20;"
```

Open issues:

```bash
sqlite3 /opt/ntpp-sentinel/data/sentinel.db \
  "select id, issue_type, phone, due_ts from issues where status='OPEN';"
```

Watermarks / internal state:

```bash
sqlite3 /opt/ntpp-sentinel/data/sentinel.db \
  "select * from kv_store;"

# Tracks internal-initiated threads by conversation
sqlite3 /opt/ntpp-sentinel/data/sentinel.db \
  "select * from conversation_state limit 20;"
```

---

## 6. If Container Won’t Start

Check logs:

```bash
docker compose logs -n 200 sentinel
```

Common causes:

- `NameError`
- Import error
- Bad merge
- Syntax error
- Missing env vars (e.g. `WEBHOOK_SECRET`, `GHL_TOKEN`, `GHL_LOCATION_ID`)

Fix locally → commit → redeploy.

---

## 7. If Webhook Returns 500

Check:

```bash
docker compose logs -n 200 sentinel
```

Look for:

- `Traceback`
- `NameError`
- `conn not defined`
- `Optional not defined`

Fix `main.py` (or offending code) → rebuild container.

---

## 8. If GHL Sending Fails

Check logs for:

- `HTTP 401` → token invalid
- `HTTP 403` → scope issue
- `HTTP 404` → wrong endpoint
- `HTTP 400` → wrong payload format

Correct send contract:

- **Endpoint**: `POST /conversations/messages`
- **Body must include**:
  - `"type": "SMS"`
  - `"message": "<text>"`
  - `"conversationId": "<id>"`
  - `"contactId": "<id>"`
- **Headers**:
  - `Authorization: Bearer <GHL_TOKEN>`
  - `Version: 2021-07-28`
  - `LocationId: <GHL_LOCATION_ID>`

---

## 9. Full Reset (Last Resort)

If system is in a weird state:

```bash
cd /opt/ntpp-sentinel
docker compose down
docker compose up -d --build
```

If DB corruption suspected:

```bash
cp data/sentinel.db data/sentinel.db.bak
```

Only wipe DB if absolutely necessary.

---

## 10. Safe Rollback

If last deploy broke prod:

```bash
cd /opt/ntpp-sentinel
git log --oneline
git checkout <previous_commit_hash>
docker compose up -d --build
```

To restore `main` afterward:

```bash
cd /opt/ntpp-sentinel
git checkout main
git pull
```

---

## 11. Production Confidence Checklist

Before walking away:

- `/health` returns `{"ok": true}`
- Manual `poll_resolver` works
- Manual `send_summary` works
- Cron log shows scheduled entries
- Summary SMS received on test manager thread

If all five are true → system stable.

---

## 12. Mental Model

Sentinel has four pillars:

1. **Webhooks** create deterministic issues (missed calls, overdue texts).
2. **Resolver** marks them resolved.
3. **Summary** reports status to managers.
4. **Cron** orchestrates timing.

Recent change: internal-initiated SMS threads are tracked per `conversation_id` and customer replies within the `INTERNAL_REPLY_GRACE_HOURS` window are **ignored** for issue creation (no false “missed text” issues). If something breaks, isolate which pillar failed.*** End Patch】}"/>
