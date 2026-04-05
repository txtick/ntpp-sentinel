# Sentinel (NTX Pool Pros)

Sentinel is the internal orchestration platform for North Texas Pool Pros.

Today it handles:
- GHL webhook intake and SLA issue tracking
- manager summaries and escalation jobs
- Route Rollover technician messaging
- Skimmer download orchestration
- Skimmer → GHL customer sync
- normalization of Skimmer data into PostgreSQL through a separate ingest worker

## Runtime

Current Docker Compose services:
- `sentinel`
- `ingest-worker`
- `caddy`

Roles:
- `sentinel`: public API, webhooks, job endpoints, customer sync, Skimmer download trigger
- `ingest-worker`: validation, `sk_*` source-ingest import, normalized upserts, derived views
- `caddy`: public reverse proxy to `sentinel:8000`

Persistence:
- `./data -> /data`
- `./logs -> /logs`

## Core Stack

- Python 3.11
- FastAPI + Uvicorn
- Docker Compose
- Caddy
- SQLite for Sentinel runtime state
- PostgreSQL for Skimmer source-ingest, normalized data, and derived analytics

## Quick Start

Create your env file:

```bash
cp .env.example .env
```

Build and start:

```bash
docker compose up -d --build
```

Redeploy on the server:

```bash
./deploy.sh
```

Check services:

```bash
docker compose ps
```

## Key Environment Variables

Required core values:

```env
WEBHOOK_SECRET=<shared_secret>
GHL_TOKEN=<leadconnector_private_integration_token>
GHL_LOCATION_ID=<ghl_location_id>
DATABASE_URL=<postgres_connection_string>
```

Required for Google Drive Skimmer sync:

```env
SKIMMER_GDRIVE_FOLDER_ID=<drive_folder_id>
SKIMMER_GDRIVE_FILE_NAME_REGEX=(?i).+\.db(\.gz)?$
GOOGLE_OAUTH_CLIENT_ID=<google_oauth_client_id>
GOOGLE_OAUTH_CLIENT_SECRET=<google_oauth_client_secret>
GOOGLE_OAUTH_REFRESH_TOKEN=<google_oauth_refresh_token>
SKIMMER_DOWNLOAD_DIR=/data/skimmer
SKIMMER_DB_PATH=/data/skimmer/skimmer.db
```

Required for Route Rollover:

```env
SKIMMER_API_BASE_URL=https://publicapi.getskimmer.com
SKIMMER_API_KEY=<skimmer_api_key>
SKIMMER_TECH_ID_MAP={}
ROLLOVER_ENABLED=1
```

Required for the ingest worker flow:

```env
INGEST_WORKER_BASE_URL=http://ingest-worker:8010
INGEST_TRIGGER_ENABLED=1
INACTIVE_PRUNE_DAYS=60
INGEST_SKIP_DUPLICATE_SOURCE_SUCCESS=1
```

`INGEST_WORKER_SECRET` may be left blank if you want it to reuse `WEBHOOK_SECRET`.

## Health

Public health:

```bash
curl https://sentinel.northtexaspoolpros.com/health
```

Worker health from inside the stack:

```bash
docker compose exec -T sentinel curl -s http://ingest-worker:8010/health
```

## Protected Endpoints

Protected routes use:

```text
X-NTPP-Secret: <WEBHOOK_SECRET>
```

## Core Jobs

Resolver:

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/poll_resolver \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Summary dry run:

```bash
curl -X POST \
  "https://sentinel.northtexaspoolpros.com/jobs/send_summary?slot=morning&dry_run=1" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Escalations:

```bash
curl -X POST \
  https://sentinel.northtexaspoolpros.com/jobs/escalations \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Skimmer download + background normalization:

```bash
curl -X POST \
  "https://sentinel.northtexaspoolpros.com/jobs/skimmer_drive_sync?import_after=1" \
  -H "X-NTPP-Secret: <WEBHOOK_SECRET>"
```

Expected behavior:
- Sentinel downloads the latest Skimmer DB to `SKIMMER_DB_PATH`
- Sentinel triggers the internal ingest worker
- ingest-worker accepts the job and runs normalization in the background

## Route Rollover

Route Rollover is a live Sentinel workflow for technician-side rollover messaging.

It:
- uses Skimmer API data to determine the assigned technician context
- maps internal GHL contacts to Skimmer tech IDs via `SKIMMER_TECH_ID_MAP`
- finds the matching customer conversation in GHL
- sends the rollover message only when a single confident conversation match exists

## Docs

Active docs:
- [`docs/Current State - Source of Truth.md`](docs/Current%20State%20-%20Source%20of%20Truth.md)
- [`docs/START UP & RECOVERY.md`](docs/START%20UP%20%26%20RECOVERY.md)
- [`docs/Sentinel - Manager Guide.md`](docs/Sentinel%20-%20Manager%20Guide.md)

Archived planning/history docs live under [`docs/archive`](docs/archive).
