import os

from fastapi import FastAPI, HTTPException, Request

from services.dashboard_backend import (
    get_dashboard_summary,
    get_alert_instance,
    get_postgres_health,
    ensure_web_backend_schema,
    list_alert_instances,
    refresh_alert_instances,
    update_alert_instance_status,
)

app = FastAPI(
    title="NTPP Web Backend",
    version="0.1.0",
)

WEB_BACKEND_SECRET = os.getenv("WEB_BACKEND_SECRET", "").strip() or os.getenv("WEBHOOK_SECRET", "").strip()


def _auth_or_401(request: Request) -> None:
    if not WEB_BACKEND_SECRET:
        raise HTTPException(status_code=500, detail="WEB_BACKEND_SECRET is not configured")
    provided = (request.headers.get("X-NTPP-Secret") or "").strip()
    if provided != WEB_BACKEND_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


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


@app.get("/api/alerts")
def api_alerts(status: str = "", category: str = "", limit: int = 100, offset: int = 0):
    return list_alert_instances(
        status=status or None,
        category=category or None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/alerts/{alert_id}")
def api_alert_detail(alert_id: int):
    return get_alert_instance(alert_id)


@app.post("/api/alerts/{alert_id}/ack")
def api_alert_ack(request: Request, alert_id: int, actor: str = "api", note: str = ""):
    _auth_or_401(request)
    return update_alert_instance_status(
        alert_id,
        next_status="acknowledged",
        actor=actor,
        note=note or None,
    )


@app.post("/api/alerts/{alert_id}/resolve")
def api_alert_resolve(request: Request, alert_id: int, actor: str = "api", note: str = ""):
    _auth_or_401(request)
    return update_alert_instance_status(
        alert_id,
        next_status="resolved",
        actor=actor,
        note=note or None,
    )


@app.post("/jobs/dashboard/refresh")
def job_dashboard_refresh(request: Request, trigger_reason: str = "manual"):
    _auth_or_401(request)
    return refresh_alert_instances(trigger_reason=trigger_reason)
