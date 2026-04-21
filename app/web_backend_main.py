import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from services.dashboard_backend import (
    create_alert_reminder,
    get_dashboard_summary,
    get_labor_payroll,
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
    update_alert_rule_config,
    create_alert_rule_config,
    delete_alert_rule_config,
    list_customers,
    list_reminders,
    list_technicians,
    list_refresh_runs,
    get_avg_water_temp,
    upsert_pollen_daily_log,
    get_pollen_daily_log,
    notify_filter_clean_customer,
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
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "https://dashboard.northtexaspoolpros.com").rstrip("/")
GOOGLE_DASHBOARD_CLIENT_ID = os.getenv("GOOGLE_DASHBOARD_CLIENT_ID", "").strip()
GOOGLE_DASHBOARD_CLIENT_SECRET = os.getenv("GOOGLE_DASHBOARD_CLIENT_SECRET", "").strip()
GOOGLE_DASHBOARD_ALLOWED_DOMAIN = os.getenv("GOOGLE_DASHBOARD_ALLOWED_DOMAIN", "northtexaspoolpros.com").strip().lower()
DASHBOARD_SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "").strip() or WEB_BACKEND_SECRET or "ntpp-dashboard-dev-session-secret"
GOOGLE_AUTH_AUTHORIZE_URL = os.getenv("GOOGLE_AUTH_AUTHORIZE_URL", "https://accounts.google.com/o/oauth2/v2/auth").strip()
GOOGLE_AUTH_TOKEN_URL = os.getenv("GOOGLE_AUTH_TOKEN_URL", "https://oauth2.googleapis.com/token").strip()
GOOGLE_AUTH_USERINFO_URL = os.getenv("GOOGLE_AUTH_USERINFO_URL", "https://openidconnect.googleapis.com/v1/userinfo").strip()

app.add_middleware(
    SessionMiddleware,
    secret_key=DASHBOARD_SESSION_SECRET,
    same_site="lax",
    https_only=DASHBOARD_BASE_URL.startswith("https://"),
    session_cookie="ntpp_dashboard_session",
    max_age=60 * 60 * 12,
)


def _dashboard_auth_enabled() -> bool:
    return bool(GOOGLE_DASHBOARD_CLIENT_ID and GOOGLE_DASHBOARD_CLIENT_SECRET)


def _secret_matches(request: Request) -> bool:
    if not WEB_BACKEND_SECRET:
        return False
    provided = (request.headers.get("X-NTPP-Secret") or "").strip()
    return provided == WEB_BACKEND_SECRET


def _dashboard_user(request: Request):
    session = request.scope.get("session") or {}
    user = session.get("dashboard_user")
    return user if isinstance(user, dict) else None


def _auth_or_401(request: Request) -> None:
    if not WEB_BACKEND_SECRET:
        raise HTTPException(status_code=500, detail="WEB_BACKEND_SECRET is not configured")
    if not _secret_matches(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _dashboard_mutation_auth_or_401(request: Request) -> None:
    if _dashboard_auth_enabled():
        if _dashboard_user(request) or _secret_matches(request):
            return
        raise HTTPException(status_code=401, detail="Unauthorized")
    _auth_or_401(request)


def _dashboard_read_auth_or_401(request: Request) -> None:
    if _dashboard_auth_enabled():
        if _dashboard_user(request) or _secret_matches(request):
            return
        raise HTTPException(
            status_code=401,
            detail={
                "message": "Dashboard login required",
                "auth_required": True,
                "login_url": "/auth/google/start",
            },
        )


_logger = __import__("logging").getLogger("web_backend")

WEATHER_LAT = float(os.getenv("WEATHER_LAT", "33.15"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "-96.82"))
AMBEE_API_KEY = os.getenv("AMBEE_API_KEY", "").strip()
_weather_cache: dict = {}
_weather_cache_ts: float = 0.0
WEATHER_CACHE_TTL = 3600  # 1 hour


@app.on_event("startup")
def _startup() -> None:
    if os.getenv("DATABASE_URL"):
        ensure_web_backend_schema()
    _check_ghl_token()


def _check_ghl_token() -> None:
    ghl_token = os.getenv("GHL_TOKEN", "").strip()
    ghl_location_id = os.getenv("GHL_LOCATION_ID", "").strip()
    if not ghl_token or not ghl_location_id:
        _logger.warning("GHL_TOKEN or GHL_LOCATION_ID not set — notify-customer will not work")
        return
    try:
        import urllib.request, urllib.error
        import urllib.parse
        qs = urllib.parse.urlencode({"locationId": ghl_location_id, "limit": "1"})
        req = urllib.request.Request(
            f"https://services.leadconnectorhq.com/conversations/search?{qs}",
            headers={
                "Authorization": f"Bearer {ghl_token}",
                "Version": "2021-07-28",
                "Accept": "application/json",
                "User-Agent": "NTPP-Sentinel/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                _logger.info("GHL token OK — conversations/search reachable")
    except urllib.error.HTTPError as exc:
        _logger.error(
            f"GHL token check failed: {exc.code} — notify-customer will return 403. "
            "Refresh GHL_TOKEN in .env and restart."
        )
    except Exception as exc:
        _logger.warning(f"GHL token check could not complete: {exc}")


@app.get("/health")
def health():
    return {"ok": True, "service": "web-backend"}


@app.get("/health/postgres")
def health_postgres():
    return get_postgres_health()


@app.get("/auth/session")
def auth_session(request: Request):
    user = _dashboard_user(request)
    return {
        "ok": True,
        "enabled": _dashboard_auth_enabled(),
        "authenticated": bool(user),
        "login_url": "/auth/google/start" if _dashboard_auth_enabled() else None,
        "user": user,
    }


@app.get("/auth/google/start")
async def auth_google_start(request: Request, next: str = "/"):
    if not _dashboard_auth_enabled():
        raise HTTPException(status_code=503, detail="Google dashboard auth is not configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = next if next.startswith("/") else "/"
    query = urlencode(
        {
            "client_id": GOOGLE_DASHBOARD_CLIENT_ID,
            "redirect_uri": f"{DASHBOARD_BASE_URL}/auth/google/callback",
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
            "hd": GOOGLE_DASHBOARD_ALLOWED_DOMAIN,
        }
    )
    return RedirectResponse(url=f"{GOOGLE_AUTH_AUTHORIZE_URL}?{query}", status_code=302)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, code: str = "", state: str = ""):
    if not _dashboard_auth_enabled():
        raise HTTPException(status_code=503, detail="Google dashboard auth is not configured")
    expected_state = (request.session.get("oauth_state") or "").strip()
    if not state or not expected_state or state != expected_state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_response = await client.post(
            GOOGLE_AUTH_TOKEN_URL,
            data={
                "client_id": GOOGLE_DASHBOARD_CLIENT_ID,
                "client_secret": GOOGLE_DASHBOARD_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{DASHBOARD_BASE_URL}/auth/google/callback",
            },
            headers={"Accept": "application/json"},
        )
        token_response.raise_for_status()
        token_payload = token_response.json()
        access_token = token_payload.get("access_token")
        if not access_token:
            raise HTTPException(status_code=401, detail="Google login did not return an access token")

        userinfo_response = await client.get(
            GOOGLE_AUTH_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        )
        userinfo_response.raise_for_status()
        profile = userinfo_response.json()

    email = str(profile.get("email") or "").strip().lower()
    email_verified = bool(profile.get("email_verified"))
    hosted_domain = str(profile.get("hd") or "").strip().lower()
    if not email or not email_verified:
        raise HTTPException(status_code=401, detail="Google account email is not verified")
    if not email.endswith(f"@{GOOGLE_DASHBOARD_ALLOWED_DOMAIN}") and hosted_domain != GOOGLE_DASHBOARD_ALLOWED_DOMAIN:
        raise HTTPException(status_code=403, detail="Google account is not in the allowed Workspace domain")

    request.session.pop("oauth_state", None)
    redirect_to = request.session.pop("oauth_next", "/")
    request.session["dashboard_user"] = {
        "email": email,
        "name": profile.get("name") or email,
        "given_name": profile.get("given_name") or "",
        "picture": profile.get("picture") or "",
        "actor": email.split("@", 1)[0],
    }
    return RedirectResponse(url=redirect_to if str(redirect_to).startswith("/") else "/", status_code=302)


@app.post("/auth/logout")
def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/home/summary")
def api_home_summary(request: Request):
    _dashboard_read_auth_or_401(request)
    return {
        "ok": True,
        "summary": get_dashboard_summary(),
    }


@app.get("/api/customers")
def api_customers(
    request: Request,
    status: str = "",
    search: str = "",
    operational_only: int = 0,
    limit: int = 100,
    offset: int = 0,
):
    _dashboard_read_auth_or_401(request)
    return list_customers(
        status=status or None,
        search=search or None,
        operational_only=bool(operational_only),
        limit=limit,
        offset=offset,
    )


@app.get("/api/customers/{customer_id}")
def api_customer_detail(request: Request, customer_id: int):
    _dashboard_read_auth_or_401(request)
    return get_customer_detail(customer_id)


@app.get("/api/technicians")
def api_technicians(
    request: Request,
    search: str = "",
    active_only: int = 0,
    with_current_assignments_only: int = 0,
    with_recent_route_activity_only: int = 0,
    field_only: int = 0,
    role_type: str = "",
    limit: int = 100,
    offset: int = 0,
):
    _dashboard_read_auth_or_401(request)
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
def api_technician_detail(request: Request, tech_id: str):
    _dashboard_read_auth_or_401(request)
    return get_technician_detail(tech_id)


@app.get("/api/labor/payroll")
def api_labor_payroll(
    request: Request,
    start_date: str = "",
    end_date: str = "",
    include_salary: int = 0,
):
    _dashboard_read_auth_or_401(request)
    return get_labor_payroll(
        start_date=start_date or None,
        end_date=end_date or None,
        include_salary=bool(include_salary),
    )


@app.get("/api/alerts")
def api_alerts(
    request: Request,
    status: str = "",
    category: str = "",
    severity: str = "",
    rule_code: str = "",
    search: str = "",
    limit: int = 100,
    offset: int = 0,
):
    _dashboard_read_auth_or_401(request)
    return list_alert_instances(
        status=status or None,
        category=category or None,
        severity=severity or None,
        rule_code=rule_code or None,
        search=search or None,
        limit=limit,
        offset=offset,
    )


@app.get("/api/alerts/{alert_id}")
def api_alert_detail(request: Request, alert_id: int):
    _dashboard_read_auth_or_401(request)
    return get_alert_instance(alert_id)


@app.get("/api/alerts/{alert_id}/events")
def api_alert_events(request: Request, alert_id: int, limit: int = 100):
    _dashboard_read_auth_or_401(request)
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
    _dashboard_mutation_auth_or_401(request)
    return create_alert_reminder(
        alert_id,
        actor=actor,
        due_at=due_at or None,
        assigned_to=assigned_to or None,
        title=title or None,
        note=note or None,
    )


@app.post("/api/alerts/{alert_id}/notify-customer")
def api_alert_notify_customer(
    request: Request,
    alert_id: int,
    actor: str = "api",
    due_at: str = "",
    assigned_to: str = "",
    note: str = "",
    sms_body: str = "",
):
    _dashboard_mutation_auth_or_401(request)
    return notify_filter_clean_customer(
        alert_id,
        actor=actor,
        due_at=due_at or None,
        assigned_to=assigned_to or None,
        note=note or None,
        sms_body=sms_body or None,
    )


@app.post("/api/alerts/{alert_id}/ack")
def api_alert_ack(request: Request, alert_id: int, actor: str = "api", note: str = ""):
    _dashboard_mutation_auth_or_401(request)
    return update_alert_instance_status(
        alert_id,
        next_status="acknowledged",
        actor=actor,
        note=note or None,
    )


@app.post("/api/alerts/{alert_id}/resolve")
def api_alert_resolve(request: Request, alert_id: int, actor: str = "api", note: str = ""):
    _dashboard_mutation_auth_or_401(request)
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
    _dashboard_mutation_auth_or_401(request)
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
def api_refresh_runs(request: Request, limit: int = 20):
    _dashboard_read_auth_or_401(request)
    return list_refresh_runs(limit=limit)


@app.get("/api/refresh-runs/{refresh_run_id}")
def api_refresh_run_detail(request: Request, refresh_run_id: int):
    _dashboard_read_auth_or_401(request)
    return get_refresh_run(refresh_run_id)


@app.get("/api/config/alerts")
def api_config_alerts(request: Request):
    _dashboard_read_auth_or_401(request)
    return list_alert_rule_configs()


@app.post("/api/config/alerts/{table}/{rule_code}/update")
async def api_config_alert_update(request: Request, table: str, rule_code: str):
    _dashboard_mutation_auth_or_401(request)
    body = await request.json()
    return update_alert_rule_config(table, rule_code, body)


@app.post("/api/config/alerts/{table}/create")
async def api_config_alert_create(request: Request, table: str):
    _dashboard_mutation_auth_or_401(request)
    body = await request.json()
    return create_alert_rule_config(table, body)


@app.post("/api/config/alerts/{table}/{rule_code}/delete")
def api_config_alert_delete(request: Request, table: str, rule_code: str):
    _dashboard_mutation_auth_or_401(request)
    return delete_alert_rule_config(table, rule_code)


@app.get("/api/reminders")
def api_reminders(
    request: Request,
    status: str = "",
    assigned_to: str = "",
    source_type: str = "",
    overdue_only: int = 0,
    search: str = "",
    limit: int = 100,
    offset: int = 0,
):
    _dashboard_read_auth_or_401(request)
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
def api_reminder_detail(request: Request, reminder_id: int):
    _dashboard_read_auth_or_401(request)
    return get_reminder_detail(reminder_id)


@app.post("/api/reminders/{reminder_id}/ack")
def api_reminder_ack(request: Request, reminder_id: int, actor: str = "api", note: str = ""):
    _dashboard_mutation_auth_or_401(request)
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
    _dashboard_mutation_auth_or_401(request)
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
    _dashboard_mutation_auth_or_401(request)
    return snooze_reminder(
        reminder_id,
        actor=actor,
        snoozed_until=snoozed_until,
        note=note or None,
    )


@app.post("/api/reminders/{reminder_id}/complete")
def api_reminder_complete(request: Request, reminder_id: int, actor: str = "api", note: str = ""):
    _dashboard_mutation_auth_or_401(request)
    return update_reminder_status(
        reminder_id,
        next_status="completed",
        actor=actor,
        note=note or None,
    )


@app.post("/api/reminders/{reminder_id}/cancel")
def api_reminder_cancel(request: Request, reminder_id: int, actor: str = "api", note: str = ""):
    _dashboard_mutation_auth_or_401(request)
    return update_reminder_status(
        reminder_id,
        next_status="canceled",
        actor=actor,
        note=note or None,
    )


@app.get("/api/weather")
def api_weather(request: Request):
    _dashboard_read_auth_or_401(request)
    global _weather_cache, _weather_cache_ts
    now = time.time()
    if _weather_cache and now - _weather_cache_ts < WEATHER_CACHE_TTL:
        return _weather_cache

    import urllib.request as _urllib_req
    import urllib.parse as _urllib_parse

    params = _urllib_parse.urlencode({
        "latitude": WEATHER_LAT,
        "longitude": WEATHER_LON,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,precipitation,uv_index",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum,wind_speed_10m_max",
        "past_days": 7,
        "forecast_days": 7,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "timezone": "America/Chicago",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        req = _urllib_req.Request(url, headers={"User-Agent": "NTPP-Sentinel/1.0"})
        with _urllib_req.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        _logger.warning(f"Weather API fetch failed: {exc}")
        if _weather_cache:
            return _weather_cache
        raise HTTPException(status_code=503, detail="Weather data temporarily unavailable")

    import datetime as _dt

    # Fetch dust from Open-Meteo Air Quality (global model, works for TX)
    dust_daily: dict = {}
    try:
        aq_params = _urllib_parse.urlencode({
            "latitude": WEATHER_LAT,
            "longitude": WEATHER_LON,
            "hourly": "dust",
            "past_days": 7,
            "forecast_days": 1,
            "timezone": "America/Chicago",
        })
        aq_req = _urllib_req.Request(
            f"https://air-quality-api.open-meteo.com/v1/air-quality?{aq_params}",
            headers={"User-Agent": "NTPP-Sentinel/1.0"},
        )
        with _urllib_req.urlopen(aq_req, timeout=10) as resp:
            aq_data = json.loads(resp.read())
        hourly = aq_data.get("hourly", {})
        for i, t in enumerate(hourly.get("time", [])):
            d = (hourly.get("dust") or [])[i] if i < len(hourly.get("dust") or []) else None
            if d is not None:
                date = t[:10]
                dust_daily[date] = max(dust_daily.get(date) or 0, d)
    except Exception as exc:
        _logger.warning(f"Open-Meteo air quality fetch failed: {exc}")

    # Fetch current pollen from Ambee (free plan: latest only, no historical)
    current_pollen: dict = {}
    if AMBEE_API_KEY:
        try:
            a_req = _urllib_req.Request(
                f"https://api.ambeedata.com/latest/pollen/by-lat-lng?lat={WEATHER_LAT}&lng={WEATHER_LON}",
                headers={"x-api-key": AMBEE_API_KEY, "Accept": "application/json"},
            )
            with _urllib_req.urlopen(a_req, timeout=10) as resp:
                a_data = json.loads(resp.read())
            entry = (a_data.get("data") or [{}])[0]
            risk = entry.get("Risk", {})
            counts = entry.get("Count", {})
            species = entry.get("Species", {})

            # Build top-species string for tree pollen (most relevant for TX)
            tree_species = species.get("Tree", {})
            top_trees = sorted(
                [(k, v) for k, v in tree_species.items() if isinstance(v, (int, float)) and v > 0],
                key=lambda x: x[1], reverse=True
            )[:3]
            tree_detail = ", ".join(f"{k.split('/')[0].strip()} {v}" for k, v in top_trees) if top_trees else ""

            current_pollen = {
                "tree_risk": risk.get("tree_pollen"),
                "grass_risk": risk.get("grass_pollen"),
                "weed_risk": risk.get("weed_pollen"),
                "tree_count": counts.get("tree_pollen"),
                "grass_count": counts.get("grass_pollen"),
                "weed_count": counts.get("weed_pollen"),
                "tree_detail": tree_detail,
                "ragweed_count": (species.get("Weed") or {}).get("Ragweed"),
                "updated_at": entry.get("updatedAt"),
            }
            # Persist today's reading so we can show pollen history
            upsert_pollen_daily_log(current_pollen)
        except Exception as exc:
            _logger.warning(f"Ambee pollen fetch failed: {exc}")

    daily = data.get("daily", {})

    # Load stored pollen history (builds up one day at a time as the widget is loaded)
    pollen_log = get_pollen_daily_log(days=7)

    # Build past-7-days environmental summary (oldest → today)
    daily_times = daily.get("time", [])
    daily_wind_max = daily.get("wind_speed_10m_max", [])
    daily_precip = daily.get("precipitation_sum", [])
    environmental = []
    for i, date in enumerate(daily_times[:8]):   # first 8 entries cover past 7 days + today
        p = pollen_log.get(date, {})
        environmental.append({
            "date": date,
            "max_wind": daily_wind_max[i] if i < len(daily_wind_max) else None,
            "precip": daily_precip[i] if i < len(daily_precip) else None,
            "max_dust": dust_daily.get(date),
            "tree_risk": p.get("tree_risk"),
            "grass_risk": p.get("grass_risk"),
            "weed_risk": p.get("weed_risk"),
        })

    # Use actual fleet water temp from chemistry readings (7-day avg across active pools).
    # Fall back to air-temp estimate only if no readings are available.
    actual_water_temp_f = get_avg_water_temp(days=7)
    if actual_water_temp_f is None:
        past_maxes = (daily.get("temperature_2m_max") or [])[:7]
        past_mins = (daily.get("temperature_2m_min") or [])[:7]
        means = [
            (hi + lo) / 2
            for hi, lo in zip(past_maxes, past_mins)
            if hi is not None and lo is not None
        ]
        estimated_water_temp_f = round(sum(means) / len(means)) if means else None
        water_temp_source = "estimated"
    else:
        estimated_water_temp_f = actual_water_temp_f
        water_temp_source = "measured"

    result = {
        "current": data.get("current", {}),
        "daily": daily,
        "environmental": environmental,
        "current_pollen": current_pollen,
        "estimated_water_temp_f": estimated_water_temp_f,
        "water_temp_source": water_temp_source,
        "fetched_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
    }
    _weather_cache = result
    _weather_cache_ts = now
    return result
