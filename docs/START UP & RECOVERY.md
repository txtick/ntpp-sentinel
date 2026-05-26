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
  docker compose logs -n 50 sentinel &&
  docker compose logs -n 50 ingest-worker &&
  docker compose logs -n 50 web-backend
'
curl -s https://sentinel.northtexaspoolpros.com/health
curl -s https://dashboard.northtexaspoolpros.com/health
```

Expected public health:

```json
{"ok": true}
```

Expected dashboard health:

```json
{"ok": true, "service": "web-backend"}
```

---

## 2. Services

Current compose services:

- `sentinel`
- `ingest-worker`
- `web-backend`
- `web-frontend`
- `caddy`

Current roles:

- `sentinel`: webhooks, jobs, customer sync, Skimmer DB download, worker trigger
- `ingest-worker`: validation, import, normalization, derived view refresh
- `web-backend`: dashboard/query API surface, alert refresh, reminders, labor view
- `web-frontend`: dashboard UI
- `caddy`: public reverse proxy to `sentinel`

Internal-only services:

- `ingest-worker`
- `web-backend`
- `web-frontend`

Public entrypoints:

- `https://sentinel.northtexaspoolpros.com` -> `sentinel`
- `https://dashboard.northtexaspoolpros.com` -> `web-frontend`

---

## 3. Core Verification Commands

Check containers:

```bash
cd /opt/ntpp-sentinel
docker compose ps
```

Check recent logs:

```bash
docker compose logs --tail=200 sentinel
docker compose logs --tail=200 ingest-worker
docker compose logs --tail=200 web-backend
```

Saved trace script for issue-flow debugging:

```bash
cd /opt/ntpp-sentinel
./trace.sh +12146323629
```

Authenticated job helper:

```bash
cd /opt/ntpp-sentinel
./curl_job.sh /jobs/verify_pending
./curl_job.sh /jobs/poll_resolver
./curl_job.sh "/jobs/recheck_issue?id=444"
./curl_job.sh "/jobs/recheck_issue?conversation_id=UHOpErKZ9wDHBlbH3PX2"
./curl_job.sh "/jobs/cleanup_raw_events?dry_run=1"
```

Notes:
- `verify_pending` is currently a compatibility wrapper around `poll_resolver`
- `verify_pending` remains available for manual compatibility checks, but cron should run `poll_resolver` directly
- manager notifications only include overdue `OPEN` issues
- `recheck_issue` forces a fresh AI gate classification for an existing issue/conversation and immediately resolves matching `OPEN` / `PENDING` issues on that conversation when the refreshed result is a confident `NO`
- use `recheck_issue?id=<issue>` when you have a Sentinel issue id, or `recheck_issue?conversation_id=<ghl_conversation_id>` when you want to re-evaluate the whole thread directly
- `dashboard/refresh` re-runs backend-owned dashboard alert detection and clears alert instances that no longer qualify
- `dashboard/refresh` also runs reminder-side filter-clean quote detection, so quote reminders can auto-complete when a matching Skimmer quote exists
- filter-clean quote reminders are also rechecked automatically by the dedicated `web-backend` quote-sync cron job during business hours
- standard Skimmer ingest refreshes Sales Assist quote tables; use the quote-only import only as a recovery shortcut when quote status looks stale
- manager summaries currently focus on overdue calls/texts plus escalated items; dashboard reminder pressure and `resolved since last summary` are omitted to keep SMS length reliable

Manual dashboard alert refresh:

```bash
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/dashboard/refresh?trigger_reason=manual" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Manual filter-clean quote sync:

```bash
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/filter-clean/quote-sync" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Manual Sales Assist quote table refresh:

```bash
docker compose exec -T sentinel sh -lc '
curl -sS -X POST \
  "http://localhost:8000/jobs/skimmer_import_quotes" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
'
```

Manual pollen snapshot:

```bash
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/weather/pollen_snapshot" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Notes:

- The dedicated `weather_pollen` cron now runs four times daily (`6:15am`, `10:15am`, `2:15pm`, `6:15pm` local) to reduce blank pollen-history days from transient Ambee failures.
- If the live Ambee request fails during a dashboard load, the widget now falls back to today's stored pollen row when one was already captured earlier that day.
- If Ambee returns `401 Unauthorized`, verify the key inside `web-backend` and direct Ambee auth before rotating anything:

```bash
docker compose exec -T web-backend sh -lc '
  echo "AMBEE_API_KEY chars: ${#AMBEE_API_KEY}"
  curl -sS -i \
    "https://api.ambeedata.com/latest/pollen/by-lat-lng?lat=${WEATHER_LAT:-33.15}&lng=${WEATHER_LON:--96.82}" \
    -H "x-api-key: $AMBEE_API_KEY" \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    | sed -n "1,20p"
'
```

To use Google Pollen instead, set these in `/opt/ntpp-sentinel/.env` and restart `web-backend`:

```bash
POLLEN_PROVIDER=google
GOOGLE_POLLEN_API_KEY=your_google_maps_pollen_key
# Optional Google forecast-day storage. Google supports up to 5 days.
# GOOGLE_POLLEN_FORECAST_DAYS=5
# Optional dashboard-load timeout tuning, in seconds:
# WEATHER_API_TIMEOUT=4
# WEATHER_AQ_TIMEOUT=3
# POLLEN_API_TIMEOUT=4
```

Google key restrictions:

- Application restriction: use the droplet/server outbound IP, not HTTP referrer. `web-backend` calls Google server-side, so no browser referrer is sent.
- API restriction: Pollen API only.

Then verify the Google Pollen key from inside `web-backend`:

```bash
docker compose exec -T web-backend sh -lc '
  echo "GOOGLE_POLLEN_API_KEY chars: ${#GOOGLE_POLLEN_API_KEY}"
  curl -sS -i \
    "https://pollen.googleapis.com/v1/forecast:lookup?key=${GOOGLE_POLLEN_API_KEY}&location.longitude=${WEATHER_LON:--96.82}&location.latitude=${WEATHER_LAT:-33.15}&days=1&plantsDescription=false" \
    | sed -n "1,20p"
'
```

With `POLLEN_PROVIDER=google`, the scheduled pollen snapshot stores today's Google pollen forecast plus the next configured forecast dates in `pollen_daily_log`. This avoids relying on dashboard traffic to create pollen history.

## 3a. Frontend Local Checks

The dashboard frontend now has a minimal local tooling setup for JS linting,
format checking, and smoke validation.

Run from a WSL shell in the repo:

```bash
cd ~/ntpp-sentinel
source "$HOME/.nvm/nvm.sh"
npm install
npm run check
```

Available scripts:

- `npm run lint`
- `npm run format:check`
- `npm run format:write`
- `npm run test`
- `npm run check`

What these cover today:

- ESLint for `web-frontend/app.js` and supporting scripts
- Prettier format validation
- a lightweight frontend smoke check for expected SPA files, selectors, and core render/init hooks

What they do not cover yet:

- browser rendering correctness
- responsive layout verification
- visual regression testing

Filter-clean notify/reminder workflow:

- use the dashboard `Notify Customer` action on filter-clean alerts when you want Sentinel to text the customer and create the quote-follow-up reminder
- Sentinel does not create the Skimmer quote directly through the public API today
- once a matching filter-clean quote exists in Skimmer, the reminder should auto-complete on the next quote-sync run or dashboard refresh

---

## 4. Skimmer Download + Normalization

Primary nightly path:

1. Sentinel downloads the latest Skimmer DB from Google Drive
2. Sentinel triggers the ingest worker
3. Worker accepts the job and runs normalization in the background

Manual trigger:

```bash
curl -i -X POST "https://sentinel.northtexaspoolpros.com/jobs/skimmer_drive_sync?import_after=1" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Expected result:
- download succeeds
- response includes `normalization.status = accepted`

Manual worker validation only:

```bash
docker compose exec -T ingest-worker python -m ingest.run \
  --sqlite /data/skimmer/skimmer.db \
  --validate-only
```

Manual full worker run:

```bash
docker compose exec -T ingest-worker python -m ingest.run \
  --sqlite /data/skimmer/skimmer.db \
  --source-system skimmer
```

Check latest pipeline runs:

```bash
docker compose exec -T sentinel python - <<'PY'
from pg import pg
with pg() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, started_at, completed_at, source_filename, success, error_message
            FROM ingest_pipeline_runs
            ORDER BY id DESC
            LIMIT 10
        """)
        for row in cur.fetchall():
            print(row)
PY
```

Check normalized table counts:

```bash
docker compose exec -T sentinel python - <<'PY'
from pg import pg
tables = ["customers", "pools", "chemistry_readings", "chemical_dose_events"]
with pg() as conn:
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(f"SELECT COUNT(*) AS count FROM {table}")
            print(table, cur.fetchone()["count"])
PY
```

Check dashboard summary view:

```bash
docker compose exec -T sentinel python - <<'PY'
from pg import pg
with pg() as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM dashboard_summary_v")
        print(cur.fetchone())
PY
```

Check current revenue opportunities directly:

```bash
docker compose exec -T sentinel python - <<'PY'
from pg import pg
with pg() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT rule_code, customer_name, pool_name, observed_count, service_date
            FROM revenue_opportunities_v
            ORDER BY customer_name, pool_name, rule_code
            LIMIT 100
        """)
        for row in cur.fetchall():
            print(row)
PY
```

---

## 5. Sentinel Queue Verification

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

Recent resolves with source attribution:

```bash
sqlite3 -header -column /opt/ntpp-sentinel/data/sentinel.db "
SELECT
  id,
  issue_type,
  status,
  resolved_ts,
  json_extract(meta,'$.resolved_by') AS resolved_by,
  json_extract(meta,'$.resolved_meta_ts') AS resolved_meta_ts,
  json_extract(meta,'$.ai_gate_confidence') AS ai_confidence
FROM issues
WHERE status='RESOLVED'
ORDER BY id DESC
LIMIT 50;
"
```

---

## 6. Environment Verification

Check important env values inside Sentinel:

```bash
docker compose exec -T sentinel sh -lc 'echo "$WEBHOOK_SECRET" | wc -c'
docker compose exec -T sentinel sh -lc 'echo "$GHL_TOKEN" | wc -c'
docker compose exec -T sentinel sh -lc 'echo "$GHL_LOCATION_ID"'
docker compose exec -T sentinel sh -lc 'echo "${GHL_CONTACT_URL_TEMPLATE:-default}"'
docker compose exec -T sentinel sh -lc 'echo "$MANAGER_CONTACT_IDS"'
docker compose exec -T sentinel sh -lc 'echo "$INTERNAL_CONTACT_IDS"'
docker compose exec -T sentinel sh -lc 'echo "$INTERNAL_USER_IDS"'
docker compose exec -T sentinel sh -lc 'echo "$DECISION_MODE"'
docker compose exec -T sentinel sh -lc 'echo "$AI_GATE_MODEL"'
docker compose exec -T sentinel sh -lc 'echo "$SKIMMER_API_BASE_URL"'
docker compose exec -T sentinel sh -lc 'echo "$SKIMMER_API_KEY" | wc -c'
```

Check ingest-worker env:

```bash
docker compose exec -T ingest-worker sh -lc 'echo "$INGEST_WORKER_BASE_URL"'
docker compose exec -T ingest-worker sh -lc 'echo "$INGEST_SOURCE_SYSTEM"'
docker compose exec -T ingest-worker sh -lc 'echo "$INACTIVE_PRUNE_DAYS"'
docker compose exec -T ingest-worker sh -lc 'echo "$INGEST_SKIP_DUPLICATE_SOURCE_SUCCESS"'
```

Check web-backend env:

```bash
docker compose exec -T web-backend sh -lc 'echo "$DATABASE_URL" | wc -c'
docker compose exec -T web-backend sh -lc 'echo "$SKIMMER_API_BASE_URL"'
docker compose exec -T web-backend sh -lc 'echo "$SKIMMER_API_KEY" | wc -c'
docker compose exec -T web-backend sh -lc 'echo "$TIMEZONE"'
```

Important Skimmer/worker env values:

- `SKIMMER_GDRIVE_FOLDER_ID`
- `SKIMMER_GDRIVE_FILE_NAME_REGEX`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REFRESH_TOKEN`
- `SKIMMER_DOWNLOAD_DIR`
- `SKIMMER_DB_PATH`
- `SKIMMER_API_BASE_URL`
- `SKIMMER_API_KEY`
- `INGEST_WORKER_BASE_URL`
- `INGEST_TRIGGER_ENABLED`
- `INACTIVE_PRUNE_DAYS`

If anything is missing, update `/opt/ntpp-sentinel/.env` and redeploy.

Route Sandbox Google Maps env:

```bash
GOOGLE_MAPS_SERVER_API_KEY=server_side_key
GOOGLE_MAPS_BROWSER_API_KEY=browser_key_optional_for_future_google_map_rendering
GOOGLE_MAPS_ENABLE_OPTIMIZATION=false
GOOGLE_MAPS_CACHE_TTL_DAYS=30
GOOGLE_MAPS_DAILY_REQUEST_LIMIT=500
GOOGLE_MAPS_DAILY_MATRIX_ELEMENT_LIMIT=3000
GOOGLE_MAPS_TRAFFIC_MODE=unaware
```

Notes:

- `GOOGLE_MAPS_SERVER_API_KEY` is backend-only. Never put it in frontend config or browser-visible URLs.
- Optimization remains disabled unless `GOOGLE_MAPS_ENABLE_OPTIMIZATION=true`.
- Route estimates default to `GOOGLE_MAPS_TRAFFIC_MODE=unaware`. Use `aware` or `aware_optimal` only when operators intentionally want traffic-aware planning estimates.
- The current Leaflet map does not need `GOOGLE_MAPS_BROWSER_API_KEY`.
- Verify safe config flags without exposing keys:

```bash
docker compose exec -T web-backend curl -s \
  "http://localhost:8020/api/routes/maps/status" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

---

## 7. Cron Verification

Sentinel cron:

```bash
docker compose exec -T sentinel sh -lc 'crontab -l'
```

Worker cron:

```bash
docker compose exec -T ingest-worker sh -lc 'crontab -l'
```

Cron logs:

```bash
tail -f /opt/ntpp-sentinel/logs/cron.log
tail -f /opt/ntpp-sentinel/logs/ingest-worker.log
docker compose logs -f web-backend
```

Key sentinel cron env:

- `CRON_DOW`
- `CRON_MORNING_HOUR`
- `CRON_MIDDAY_HOUR`
- `CRON_AFTERNOON_HOUR`
- `CRON_BUSINESS_HOURS`
- `CRON_BUSINESS_END_HOUR`
- `CRON_ESCALATIONS_EVERY_MINUTES`
- `CRON_POLL_RESOLVER_EVERY_MINUTES`
- `CRON_SKIMMER_SYNC_HOUR`
- `CRON_SKIMMER_SYNC_MINUTE`
- `CRON_SKIMMER_SYNC_DOW`

Key worker fallback cron env:

- `CRON_INGEST_WORKER_MINUTE`
- `CRON_INGEST_WORKER_HOUR`
- `CRON_INGEST_WORKER_DOW`

---

## 8. Price Increase Push To Skimmer

Dry run one location first:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv \
  --service-location-id E96F4668-53AE-4251-970C-6A231CFC595C
```

Dry run the full approved file:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv
```

Apply one location first:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv \
  --service-location-id E96F4668-53AE-4251-970C-6A231CFC595C \
  --apply
```

Apply the full approved file:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv \
  --apply
```

Notes:

- source of truth is CSV column `L` / `final_new_rate`
- only rows with `approved_for_increase=yes` are eligible by default
- rows with blank `final_new_rate` are skipped
- updates are keyed by `service_location_id`
- the script reads the live Skimmer service location first, preserves the existing `rateType`, and only changes the numeric rate

---

## 9. Recovery Notes

If Skimmer download succeeds but normalization does not:

1. inspect `docker compose logs --tail=200 ingest-worker`
2. inspect `ingest_pipeline_runs`
3. rerun the worker manually with `python -m ingest.run`

If the worker is unhealthy:

```bash
cd /opt/ntpp-sentinel
docker compose up -d --build --remove-orphans ingest-worker
```

If the whole stack needs a clean restart:

```bash
cd /opt/ntpp-sentinel
docker compose up -d --build --remove-orphans
```

If dashboard alerts look stale, or a newly scheduled work order should suppress an alert:

1. verify the latest Skimmer import completed
2. verify the relevant row exists in normalized tables / views
3. run a dashboard alert refresh:

```bash
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/dashboard/refresh?trigger_reason=manual" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

4. inspect recent dashboard refresh runs:

```bash
docker compose exec -T sentinel python - <<'PY'
from pg import pg
with pg() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, started_at, completed_at, trigger_reason, success, error_message, metrics_json
            FROM alert_refresh_runs
            ORDER BY id DESC
            LIMIT 10
        """)
        for row in cur.fetchall():
            print(row)
PY
```

Note:

- Some scheduled Skimmer work orders carry a placeholder `complete_time` around `2010-01-01` instead of `NULL`.
- Treat those as not completed yet when debugging upcoming-work suppression logic.
- Dashboard alerts intentionally suppress customers whose route assignment end date is within the next 30 days.
- Dashboard alerts also suppress customers carrying the Skimmer tag `no-sentinel-alerts` after the next import and dashboard refresh.

## 9. Operator Backend Paths

Operator-relevant backend paths:

- Sentinel:
  - `GET /health`
  - `GET /health/postgres`
  - `POST /jobs/poll_resolver`
  - `POST /jobs/verify_pending`
  - `POST /jobs/recheck_issue`
  - `POST /jobs/cleanup_raw_events`
  - `POST /jobs/skimmer_link`
  - `POST /jobs/skimmer_import`
  - `POST /jobs/skimmer_customer_sync`
  - `POST /jobs/skimmer_drive_sync`
  - `POST /jobs/send_summary`
  - `POST /jobs/escalations`
- Web backend:
  - `GET /health`
  - `GET /health/postgres`
  - `GET /api/home/summary`
  - `GET /api/customers`
  - `GET /api/customers/{customer_id}`
  - `GET /api/technicians`
  - `GET /api/technicians/{tech_id}`
  - `GET /api/labor/payroll`
  - `GET /api/alerts`
  - `GET /api/alerts/{alert_id}`
  - `GET /api/alerts/{alert_id}/events`
  - `POST /api/alerts/{alert_id}/reminder`
  - `POST /api/alerts/{alert_id}/notify-customer`
  - `POST /api/alerts/{alert_id}/ack`
  - `POST /api/alerts/{alert_id}/resolve`
  - `POST /api/alerts/{alert_id}/snooze`
  - `POST /jobs/dashboard/refresh`
  - `GET /api/refresh-runs`
  - `GET /api/refresh-runs/{refresh_run_id}`
  - `GET /api/config/alerts`
  - `GET /api/reminders`
  - `GET /api/reminders/{reminder_id}`
  - `POST /api/reminders/{reminder_id}/ack`
  - `POST /api/reminders/{reminder_id}/update`
  - `POST /api/reminders/{reminder_id}/snooze`
  - `POST /api/reminders/{reminder_id}/complete`
  - `POST /api/reminders/{reminder_id}/cancel`

Reminder note:

- Sentinel manager summaries currently omit dashboard reminder pressure and `resolved since last summary` so they stay under the current GHL/LeadConnector SMS size limits.
- Ingest worker:
  - `GET /health`
  - `POST /jobs/run`
