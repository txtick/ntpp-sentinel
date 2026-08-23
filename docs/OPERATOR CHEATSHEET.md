# Operator Cheatsheet

Copy-paste commands only.

## Deploy

Phone/Codex release from WSL:

```bash
cd ~/ntpp-sentinel
./release.sh "commit message"
```

Server-only redeploy:

```bash
cd /opt/ntpp-sentinel
./deploy.sh
```

## Health

```bash
curl -s https://sentinel.northtexaspoolpros.com/health
curl -s https://dashboard.northtexaspoolpros.com/health
cd /opt/ntpp-sentinel && docker compose ps
```

## Logs

```bash
cd /opt/ntpp-sentinel
docker compose logs --tail=200 sentinel
docker compose logs --tail=200 ingest-worker
docker compose logs --tail=200 web-backend
```

## Issue Debugging

```bash
cd /opt/ntpp-sentinel
./trace.sh +12146323629 --summary --save
```

## Resolver / AI Recheck

```bash
cd /opt/ntpp-sentinel
./curl_job.sh /jobs/poll_resolver
./curl_job.sh "/jobs/recheck_issue?id=444"
./curl_job.sh "/jobs/recheck_issue?conversation_id=UHOpErKZ9wDHBlbH3PX2"
```

Filter-clean quote reminder refresh:

```bash
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/dashboard/refresh?trigger_reason=manual" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Use this after a filter-clean quote has been created if you want reminder cleanup to happen immediately.

Dedicated filter-clean quote sync:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/filter-clean/quote-sync" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Sales Assist quote table refresh:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel sh -lc '
curl -sS -X POST \
  "http://localhost:8000/jobs/skimmer_import_quotes" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
'
```

## Dashboard Alert Refresh

```bash
cd /opt/ntpp-sentinel
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/dashboard/refresh?trigger_reason=manual" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

## Route Sandbox Maps Status

```bash
cd /opt/ntpp-sentinel
docker compose exec -T web-backend curl -s \
  "http://localhost:8020/api/routes/maps/status" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

Route optimization stays disabled unless this is set and `web-backend` is restarted:

```bash
GOOGLE_MAPS_ENABLE_OPTIMIZATION=true
GOOGLE_MAPS_TRAFFIC_MODE=unaware
```

## Manual Pollen Snapshot

```bash
cd /opt/ntpp-sentinel
docker compose exec -T web-backend curl -s -X POST \
  "http://localhost:8020/jobs/weather/pollen_snapshot" \
  -H "X-NTPP-Secret: $WEBHOOK_SECRET"
```

- Scheduled pollen snapshots run at `6:15am`, `10:15am`, `2:15pm`, and `6:15pm` local.

## Ambee Pollen Auth Check

```bash
cd /opt/ntpp-sentinel
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

## Google Pollen Setup

Set these in `/opt/ntpp-sentinel/.env`, then restart `web-backend`:

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
- Application restriction: use the droplet/server outbound IP, not HTTP referrer.
- API restriction: Pollen API only.

Direct auth check:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T web-backend sh -lc '
  echo "GOOGLE_POLLEN_API_KEY chars: ${#GOOGLE_POLLEN_API_KEY}"
  curl -sS -i \
    "https://pollen.googleapis.com/v1/forecast:lookup?key=${GOOGLE_POLLEN_API_KEY}&location.longitude=${WEATHER_LON:--96.82}&location.latitude=${WEATHER_LAT:-33.15}&days=1&plantsDescription=false" \
    | sed -n "1,20p"
'
```

## Frontend Checks

```bash
cd ~/ntpp-sentinel
source "$HOME/.nvm/nvm.sh"
npm install
npm run check
```

## Skimmer Download + Import

```bash
cd /opt/ntpp-sentinel
./curl_job.sh "/jobs/skimmer_drive_sync?import_after=1"
```

## Skimmer Price Increase Dry Run

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv
```

## Skimmer Price Increase Apply One Location

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv \
  --service-location-id E96F4668-53AE-4251-970C-6A231CFC595C \
  --apply
```

## Skimmer Price Increase Apply Full File

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel python /app/scripts/skimmer_update_service_rates_from_csv.py \
  --csv /app/sri_042026_price_export.csv \
  --apply
```

## Validate Worker Against Current SQLite

```bash
cd /opt/ntpp-sentinel
docker compose exec -T ingest-worker python -m ingest.run \
  --sqlite /data/skimmer/skimmer.db \
  --validate-only
```

## Labor Compare

Live Skimmer API:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel sh -lc 'python /app/scripts/labor_compare_skimmer_api.py --start 2026-04-12 --end 2026-04-18 --debug-first-day'
```

Nightly Skimmer SQLite:

```bash
cd /opt/ntpp-sentinel
docker compose exec -T sentinel sh -lc 'python /app/scripts/labor_compare_skimmer_sqlite.py --sqlite /data/skimmer/skimmer.db --start 2026-04-12 --end 2026-04-18'
```

## Check Recent Pipeline Runs

```bash
cd /opt/ntpp-sentinel
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

## Tag-Based Dashboard Alert Suppression

Add this Skimmer tag to the customer:

```text
no-sentinel-alerts
```

Then wait for import, or refresh after the next DB download.
