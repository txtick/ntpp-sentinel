import os

from fastapi import FastAPI, HTTPException, Request

from services.dashboard_backend import (
    get_dashboard_summary,
    get_alert_instance,
    get_customer_detail,
    get_refresh_run,
    get_postgres_health,
    ensure_web_backend_schema,
    list_alert_events,
    list_alert_instances,
    list_alert_rule_configs,
    list_customers,
    list_refresh_runs,
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


@app.get("/api/customers")
def api_customers(
    status: str = "",
    search: str = "",
    operational_only: int = 0,
    limit: int = 100,
    offset: int = 0,
):
    return list_customers(
        status=status or None,
        search=search or None,
        operational_only=bool(operational_only),
        limit=limit,
        offset=offset,
    )


@app.get("/api/customers/{customer_id}")
def api_customer_detail(customer_id: int):
    return get_customer_detail(customer_id)


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


@app.get("/api/alerts/{alert_id}/events")
def api_alert_events(alert_id: int, limit: int = 100):
    return list_alert_events(alert_id, limit=limit)


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


@app.get("/api/refresh-runs")
def api_refresh_runs(limit: int = 20):
    return list_refresh_runs(limit=limit)


@app.get("/api/refresh-runs/{refresh_run_id}")
def api_refresh_run_detail(refresh_run_id: int):
    return get_refresh_run(refresh_run_id)


@app.get("/api/config/alerts")
def api_config_alerts():
    return list_alert_rule_configs()
