import json
import os
import subprocess
import sys
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request

WORKER_SECRET = os.getenv("INGEST_WORKER_SECRET", "").strip() or os.getenv("WEBHOOK_SECRET", "")
DEFAULT_SOURCE_SYSTEM = os.getenv("INGEST_SOURCE_SYSTEM", "skimmer")
WORKER_LOG_PATH = os.getenv("INGEST_WORKER_LOG_PATH", "/logs/ingest-worker.log")

app = FastAPI(title="NTPP Ingest Worker", version="1.0.0", redoc_url=None)


def _auth_or_401(request: Request) -> None:
    if not WORKER_SECRET:
        raise HTTPException(status_code=500, detail="INGEST_WORKER_SECRET is not configured")
    provided = request.headers.get("X-NTPP-Secret", "")
    if provided != WORKER_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "service": "ingest-worker"}


@app.post("/jobs/run", status_code=202)
async def run_job(request: Request) -> Dict[str, Any]:
    _auth_or_401(request)
    payload: Dict[str, Any] = {}
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    sqlite_path = str(payload.get("sqlite_path") or os.getenv("SKIMMER_DB_PATH") or "").strip()
    source_system = str(payload.get("source_system") or DEFAULT_SOURCE_SYSTEM).strip() or DEFAULT_SOURCE_SYSTEM
    trigger_reason = str(payload.get("trigger_reason") or "worker_api").strip() or "worker_api"
    trigger_metadata = payload.get("trigger_metadata")
    if trigger_metadata is not None and not isinstance(trigger_metadata, dict):
        trigger_metadata = {"value": trigger_metadata}
    if not sqlite_path:
        raise HTTPException(status_code=400, detail="sqlite_path is required")

    cmd = [
        sys.executable,
        "-m",
        "ingest.run",
        "--sqlite",
        sqlite_path,
        "--source-system",
        source_system,
        "--trigger-reason",
        trigger_reason,
        "--trigger-metadata-json",
        json.dumps(trigger_metadata or {}),
    ]

    try:
        os.makedirs(os.path.dirname(WORKER_LOG_PATH) or "/logs", exist_ok=True)
        with open(WORKER_LOG_PATH, "ab") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
                cwd="/app",
            )
        return {
            "status": "accepted",
            "pid": proc.pid,
            "sqlite_path": sqlite_path,
            "source_system": source_system,
            "trigger_reason": trigger_reason,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to spawn worker pipeline: {exc}")
