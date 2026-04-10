import os

from fastapi import FastAPI, HTTPException, Request

from services.dashboard_backend import (
    create_alert_reminder,
    get_dashboard_summary,
    get_alert_instance,
    get_customer_detail,
    get_refresh_run,
    get_reminder_detail,
    get_technician_detail,
    get_postgres_health,
    ensure_web_backend_schema,
    list_alert_events,
    list_alert_instances,
    list_alert_rule_configs,
    list_customers,
    list_reminders,
    list_technicians,
    list_refresh_runs,
    refresh_alert_instances,
    snooze_reminder,
    update_alert_instance_status,
    update_reminder_fields,
    update_reminder_status,
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


@app.get("/api/technicians")
def api_technicians(
    search: str = "",
    active_only: int = 0,
    with_current_assignments_only: int = 0,
    with_recent_route_activity_only: int = 0,
    field_only: int = 0,
    role_type: str = "",
    limit: int = 100,
    offset: int = 0,
):
    return list_technicians(
        search=search or None,
        active_only=bool(active_only),
        with_current_assignments_only=bool(with_current_assignments_only),
        with_recent_route_activity_only=bool(with_recent_route_activity_only),
        field_only=bool(field_only),
        role_type=role_type or None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/technicians/{tech_id}")
def api_technician_detail(tech_id: str):
    return get_technician_detail(tech_id)


@app.get("/api/alerts")
def api_alerts(
    status: str = "",
    category: str = "",
    rule_code: str = "",
    search: str = "",
    limit: int = 100,
    offset: int = 0,
):
    return list_alert_instances(
        status=status or None,
        category=category or None,
        rule_code=rule_code or None,
        search=search or None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/alerts/{alert_id}")
def api_alert_detail(alert_id: int):
    return get_alert_instance(alert_id)


@app.get("/api/alerts/{alert_id}/events")
def api_alert_events(alert_id: int, limit: int = 100):
    return list_alert_events(alert_id, limit=limit)


@app.post("/api/alerts/{alert_id}/reminder")
def api_alert_create_reminder(
    request: Request,
    alert_id: int,
    actor: str = "api",
    due_at: str = "",
    assigned_to: str = "",
    title: str = "",
    note: str = "",
):
    _auth_or_401(request)
    return create_alert_reminder(
        alert_id,
        actor=actor,
        due_at=due_at or None,
        assigned_to=assigned_to or None,
        title=title or None,
        note=note or None,
    )


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


@app.post("/api/alerts/{alert_id}/snooze")
def api_alert_snooze(
    request: Request,
    alert_id: int,
    snoozed_until: str,
    actor: str = "api",
    note: str = "",
):
    _auth_or_401(request)
    return update_alert_instance_status(
        alert_id,
        next_status="snoozed",
        actor=actor,
        note=note or None,
        snoozed_until=snoozed_until,
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


@app.get("/api/reminders")
def api_reminders(
    status: str = "",
    assigned_to: str = "",
    source_type: str = "",
    overdue_only: int = 0,
    search: str = "",
    limit: int = 100,
    offset: int = 0,
):
    return list_reminders(
        status=status or None,
        assigned_to=assigned_to or None,
        source_type=source_type or None,
        overdue_only=bool(overdue_only),
        search=search or None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/reminders/{reminder_id}")
def api_reminder_detail(reminder_id: int):
    return get_reminder_detail(reminder_id)


@app.post("/api/reminders/{reminder_id}/ack")
def api_reminder_ack(request: Request, reminder_id: int, actor: str = "api", note: str = ""):
    _auth_or_401(request)
    return update_reminder_status(
        reminder_id,
        next_status="acknowledged",
        actor=actor,
        note=note or None,
    )


@app.post("/api/reminders/{reminder_id}/update")
def api_reminder_update(
    request: Request,
    reminder_id: int,
    actor: str = "api",
    assigned_to: str = "",
    due_at: str = "",
    title: str = "",
    note: str = "",
):
    _auth_or_401(request)
    return update_reminder_fields(
        reminder_id,
        actor=actor,
        assigned_to=assigned_to if assigned_to != "" else None,
        due_at=due_at if due_at != "" else None,
        title=title if title != "" else None,
        note=note if note != "" else None,
    )


@app.post("/api/reminders/{reminder_id}/snooze")
def api_reminder_snooze(
    request: Request,
    reminder_id: int,
    snoozed_until: str,
    actor: str = "api",
    note: str = "",
):
    _auth_or_401(request)
    return snooze_reminder(
        reminder_id,
        actor=actor,
        snoozed_until=snoozed_until,
        note=note or None,
    )


@app.post("/api/reminders/{reminder_id}/complete")
def api_reminder_complete(request: Request, reminder_id: int, actor: str = "api", note: str = ""):
    _auth_or_401(request)
    return update_reminder_status(
        reminder_id,
        next_status="completed",
        actor=actor,
        note=note or None,
    )


@app.post("/api/reminders/{reminder_id}/cancel")
def api_reminder_cancel(request: Request, reminder_id: int, actor: str = "api", note: str = ""):
    _auth_or_401(request)
    return update_reminder_status(
        reminder_id,
        next_status="canceled",
        actor=actor,
        note=note or None,
    )
