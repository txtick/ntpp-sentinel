import os

from fastapi import FastAPI

from services.dashboard_backend import (
    get_dashboard_summary,
    get_postgres_health,
    ensure_web_backend_schema,
)

app = FastAPI(
    title="NTPP Web Backend",
    version="0.1.0",
)


@app.on_event("startup")
def _startup() -> None:
    if os.getenv("DATABASE_URL"):
        ensure_web_backend_schema()


@app.get("/health")
def health():
    return {"ok": True, "service": "web-backend"}


@app.get("/health/postgres")
def health_postgres():
    return get_postgres_health()


@app.get("/api/home/summary")
def api_home_summary():
    return {
        "ok": True,
        "summary": get_dashboard_summary(),
    }
