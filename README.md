# Sentinel (NTX Pool Pros)

Sentinel is an automation/orchestration service that ingests GoHighLevel webhooks and produces deterministic manager rollups (no real-time alert spam).

## Architecture
- FastAPI + Uvicorn (Python 3.11)
- Docker Compose
- SQLite persisted to `./data` (volume mounted to `/data`)
- Logs persisted to `./logs` (volume mounted to `/logs`)
- Caddy reverse proxy

## Run
1. Copy env file:
   ```bash
   cp .env.example .env
   # edit .env with real values
