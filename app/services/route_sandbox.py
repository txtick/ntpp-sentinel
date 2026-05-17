import json
import logging
import math
import os
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException

from pg import DATABASE_URL, ensure_route_sandbox_schema, pg

_logger = logging.getLogger("route_sandbox")

DAY_ORDER = {
    "Monday": 0,
    "Tuesday": 1,
    "Wednesday": 2,
    "Thursday": 3,
    "Friday": 4,
    "Saturday": 5,
    "Sunday": 6,
}

SCENARIO_STATUSES = {"draft", "locked", "approved", "manually_completed", "archived"}
SCENARIO_EDITABLE_STATUSES = {"draft"}
SCENARIO_STATUS_TRANSITIONS = {
    "draft": {"locked", "approved", "archived"},
    "locked": {"approved", "archived"},
    "approved": {"manually_completed", "archived"},
    "manually_completed": {"archived"},
    "archived": set(),
}
CHANGE_PLAN_STATUSES = {
    "generated",
    "approved",
    "printed",
    "manually_in_progress",
    "manually_completed",
    "failed",
    "partially_completed",
    "stale",
}
CHANGE_PLAN_ACTIONABLE_STATUSES = {"approved", "printed", "manually_in_progress"}
VALID_SOURCE_SYSTEMS = {"skimmer"}

# Warn if a route group exceeds this many stops
WARN_POOL_COUNT_THRESHOLD = int(__import__("os").getenv("ROUTE_WARN_POOL_COUNT", "20"))
# Warn if estimated route hours exceed this
WARN_ROUTE_HOURS_THRESHOLD = float(__import__("os").getenv("ROUTE_WARN_HOURS", "8.0"))
# Warn if total drive miles estimate exceeds this (rough, no real routing)
WARN_ROUTE_MILES_THRESHOLD = float(__import__("os").getenv("ROUTE_WARN_MILES", "120.0"))
# Outlier distance threshold in miles (stop far from group centroid)
WARN_OUTLIER_MILES = float(__import__("os").getenv("ROUTE_WARN_OUTLIER_MILES", "15.0"))
# Warn if start/end drive from tech home exceeds this
WARN_COMMUTE_MILES = float(__import__("os").getenv("ROUTE_WARN_COMMUTE_MILES", "30.0"))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _google_maps_daily_request_limit() -> int:
    return max(0, _env_int("GOOGLE_MAPS_DAILY_REQUEST_LIMIT", 500))


def _google_maps_daily_matrix_element_limit() -> int:
    return max(0, _env_int("GOOGLE_MAPS_DAILY_MATRIX_ELEMENT_LIMIT", 3000))


def _require_postgres() -> None:
    if not DATABASE_URL:
        raise HTTPException(status_code=404, detail="DATABASE_URL is not configured")


def _validate_source_system(source_system: str) -> str:
    value = str(source_system or "skimmer").strip().lower()
    if value not in VALID_SOURCE_SYSTEMS:
        raise HTTPException(status_code=400, detail="Only imported Skimmer route data is supported")
    return value


def _require_valid_day(day: Any) -> str:
    value = str(day or "").strip()
    if value not in DAY_ORDER:
        raise HTTPException(status_code=400, detail=f"day_of_week must be one of: {', '.join(DAY_ORDER.keys())}")
    return value


def _route_change_plan_status(status: Any) -> str:
    value = str(status or "").strip()
    if value not in CHANGE_PLAN_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(sorted(CHANGE_PLAN_STATUSES))}")
    return value


def _haversine_miles(lat1: Optional[float], lon1: Optional[float], lat2: Optional[float], lon2: Optional[float]) -> Optional[float]:
    if any(v is None for v in (lat1, lon1, lat2, lon2)):
        return None
    R = 3958.8
    lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _route_miles(stops: List[Dict[str, Any]]) -> float:
    total = 0.0
    prev_lat = prev_lon = None
    for stop in stops:
        lat = stop.get("latitude")
        lon = stop.get("longitude")
        if lat is not None and lon is not None:
            if prev_lat is not None:
                d = _haversine_miles(prev_lat, prev_lon, lat, lon)
                if d is not None:
                    total += d
            prev_lat, prev_lon = float(lat), float(lon)
    return round(total, 2)


def _route_endpoint(
    tech_profile: Optional[Dict[str, Any]],
    kind: str,
) -> Tuple[Optional[float], Optional[float], str]:
    profile = tech_profile or {}
    location_type = profile.get(
        "default_start_location_type" if kind == "start" else "default_end_location_type",
        "first_stop" if kind == "start" else "last_stop",
    )
    if location_type == "home":
        return profile.get("home_latitude"), profile.get("home_longitude"), "home"
    if location_type == "custom":
        return (
            profile.get("custom_start_latitude" if kind == "start" else "custom_end_latitude"),
            profile.get("custom_start_longitude" if kind == "start" else "custom_end_longitude"),
            "custom",
        )
    if location_type == "office":
        office_lat = os.getenv("ROUTE_OFFICE_LATITUDE") or os.getenv("ROUTE_OFFICE_LAT")
        office_lng = os.getenv("ROUTE_OFFICE_LONGITUDE") or os.getenv("ROUTE_OFFICE_LON")
        if office_lat and office_lng:
            return float(office_lat), float(office_lng), "office"
    return None, None, location_type


def _centroid(stops: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    lats = [float(s["latitude"]) for s in stops if s.get("latitude") is not None]
    lons = [float(s["longitude"]) for s in stops if s.get("longitude") is not None]
    if not lats:
        return None, None
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _build_route_warnings(
    stops: List[Dict[str, Any]],
    tech_profile: Optional[Dict[str, Any]] = None,
) -> List[str]:
    warnings: List[str] = []
    n = len(stops)
    if n > WARN_POOL_COUNT_THRESHOLD:
        warnings.append(f"Route has {n} stops, which exceeds the recommended maximum of {WARN_POOL_COUNT_THRESHOLD}.")

    estimated_hours = sum(s.get("estimated_duration_minutes") or 45 for s in stops) / 60.0
    if estimated_hours > WARN_ROUTE_HOURS_THRESHOLD:
        warnings.append(f"Estimated service time is {estimated_hours:.1f}h, which exceeds the {WARN_ROUTE_HOURS_THRESHOLD:.0f}h target.")

    ordered = sorted(stops, key=lambda s: s.get("stop_order") or 9999)
    miles = _route_miles(ordered)
    if miles > WARN_ROUTE_MILES_THRESHOLD:
        warnings.append(f"Estimated drive distance is {miles:.0f} mi, which may be high.")

    clat, clon = _centroid(ordered)
    if clat is not None:
        for stop in ordered:
            d = _haversine_miles(clat, clon, stop.get("latitude"), stop.get("longitude"))
            if d is not None and d > WARN_OUTLIER_MILES:
                name = stop.get("customer_name") or stop.get("address") or "Unknown"
                warnings.append(f"{name} is {d:.1f} mi from the route centroid — possible geographic outlier.")

    missing_coords = [s for s in stops if s.get("latitude") is None or s.get("longitude") is None]
    if missing_coords:
        warnings.append(f"{len(missing_coords)} stop(s) are missing GPS coordinates and will not appear on the map.")

    if tech_profile:
        start_type = tech_profile.get("default_start_location_type", "first_stop")
        end_type = tech_profile.get("default_end_location_type", "last_stop")
        include_start = bool(tech_profile.get("include_start_drive_in_metrics"))
        include_end = bool(tech_profile.get("include_end_drive_in_metrics"))
        if ordered and include_start and start_type in ("home", "custom"):
            slat, slon, _ = _route_endpoint(tech_profile, "start")
            first = next((s for s in ordered if s.get("latitude") is not None), None)
            if first and slat is not None and slon is not None:
                d = _haversine_miles(slat, slon, first.get("latitude"), first.get("longitude"))
                if d is not None and d > WARN_COMMUTE_MILES:
                    warnings.append(f"First stop is {d:.1f} mi from tech start location — long commute.")
        if ordered and include_end and end_type in ("home", "custom"):
            elat, elon, _ = _route_endpoint(tech_profile, "end")
            last = next((s for s in reversed(ordered) if s.get("latitude") is not None), None)
            if last and elat is not None and elon is not None:
                d = _haversine_miles(elat, elon, last.get("latitude"), last.get("longitude"))
                if d is not None and d > WARN_COMMUTE_MILES:
                    warnings.append(f"Last stop is {d:.1f} mi from tech end location — long return drive.")

    return warnings


def _build_route_metrics(
    stops: List[Dict[str, Any]],
    tech_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    ordered = sorted(stops, key=lambda s: s.get("stop_order") or 9999)
    in_route_miles = _route_miles(ordered)
    service_minutes = sum(s.get("estimated_duration_minutes") or 45 for s in stops)

    start_to_first_stop_miles: Optional[float] = None
    last_stop_to_end_miles: Optional[float] = None
    start_type = (tech_profile or {}).get("default_start_location_type", "first_stop")
    end_type = (tech_profile or {}).get("default_end_location_type", "last_stop")
    include_start = bool((tech_profile or {}).get("include_start_drive_in_metrics"))
    include_end = bool((tech_profile or {}).get("include_end_drive_in_metrics"))

    if tech_profile and start_type in ("home", "custom"):
        slat, slon, _ = _route_endpoint(tech_profile, "start")
        first = next((s for s in ordered if s.get("latitude") is not None), None)
        if first and slat is not None and slon is not None:
            start_to_first_stop_miles = _haversine_miles(slat, slon, first.get("latitude"), first.get("longitude"))

    if tech_profile and end_type in ("home", "custom"):
        elat, elon, _ = _route_endpoint(tech_profile, "end")
        last = next((s for s in reversed(ordered) if s.get("latitude") is not None), None)
        if last and elat is not None and elon is not None:
            last_stop_to_end_miles = _haversine_miles(elat, elon, last.get("latitude"), last.get("longitude"))

    start_weighted = start_to_first_stop_miles if include_start else 0.0
    end_weighted = last_stop_to_end_miles if include_end else 0.0
    commute_miles = (start_weighted or 0.0) + (end_weighted or 0.0)
    total_without_start_end = round(in_route_miles, 2)
    total_with_start_end = round(in_route_miles + (start_to_first_stop_miles or 0.0) + (last_stop_to_end_miles or 0.0), 2)
    total_miles = round(in_route_miles + commute_miles, 2)
    # Rough estimate: 25 mph avg
    drive_time_minutes = round((total_miles / 25.0) * 60)
    total_time_minutes = service_minutes + drive_time_minutes

    return {
        "pool_count": len(stops),
        "service_minutes": service_minutes,
        "stop_to_stop_miles": round(in_route_miles, 2),
        "in_route_miles": round(in_route_miles, 2),
        "start_to_first_stop_miles": round(start_to_first_stop_miles, 2) if start_to_first_stop_miles is not None else None,
        "last_stop_to_end_miles": round(last_stop_to_end_miles, 2) if last_stop_to_end_miles is not None else None,
        "start_miles": round(start_to_first_stop_miles, 2) if start_to_first_stop_miles is not None else None,
        "end_miles": round(last_stop_to_end_miles, 2) if last_stop_to_end_miles is not None else None,
        "commute_miles": round(commute_miles, 2),
        "total_without_start_end_miles": total_without_start_end,
        "total_with_start_end_miles": total_with_start_end,
        "total_weighted_miles": total_miles,
        "total_miles": total_miles,
        "include_start_drive_in_metrics": include_start,
        "include_end_drive_in_metrics": include_end,
        "drive_time_minutes": drive_time_minutes,
        "total_time_minutes": total_time_minutes,
        "missing_coords_count": sum(1 for s in stops if s.get("latitude") is None or s.get("longitude") is None),
    }


def _ensure_scenario_editable(cur, scenario_id: int) -> Dict[str, Any]:
    cur.execute("SELECT * FROM route_scenarios WHERE id = %s", (scenario_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Scenario not found")
    scenario = dict(row)
    if scenario.get("status") not in SCENARIO_EDITABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="Scenario is read-only. Duplicate it into a new draft before editing.",
        )
    return scenario


def _mark_change_plans_stale(cur, scenario_id: int) -> None:
    cur.execute(
        """
        UPDATE route_change_plans
        SET status = 'stale',
            error_json = jsonb_build_object('reason', 'Scenario changed after packet generation')
        WHERE scenario_id = %s
          AND status IN ('generated', 'approved', 'printed')
        """,
        (scenario_id,),
    )


def _mark_route_estimates_stale(cur, scenario_id: int, technician_id: Optional[str] = None, day_of_week: Optional[str] = None) -> None:
    filters = ["scenario_id = %s", "is_stale = FALSE"]
    params: List[Any] = [scenario_id]
    if technician_id:
        filters.append("technician_id = %s")
        params.append(technician_id)
    if day_of_week:
        filters.append("service_day = %s")
        params.append(day_of_week)
    cur.execute(
        f"""
        UPDATE route_estimate_runs
        SET is_stale = TRUE, updated_at = NOW()
        WHERE {" AND ".join(filters)}
        """,
        params,
    )


# ── Current routes (from Skimmer data) ────────────────────────────────────────

def list_current_route_pools(source_system: str = "skimmer") -> Dict[str, Any]:
    _require_postgres()
    source_system = _validate_source_system(source_system)
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ra.source_route_assignment_id,
                    ra.source_service_location_id,
                    ra.source_account_id,
                    ra.day_of_week,
                    ra.sequence AS stop_order,
                    ra.frequency,
                    ra.start_date,
                    ra.end_date,
                    sl.address,
                    sl.city,
                    sl.latitude,
                    sl.longitude,
                    sl.minutes_at_stop AS estimated_duration_minutes,
                    sl.rate,
                    sl.rate_type,
                    sl.source_customer_id,
                    sl.notes AS location_notes,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                        c.company_name,
                        'Unknown Customer'
                    ) AS customer_name,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', a.first_name, a.last_name)), ''),
                        a.username,
                        ra.source_account_id
                    ) AS tech_name
                FROM sk_route_assignment ra
                LEFT JOIN sk_service_location sl
                    ON ra.source_system = sl.source_system
                   AND ra.source_service_location_id = sl.source_location_id
                LEFT JOIN sk_customer c
                    ON sl.source_system = c.source_system
                   AND sl.source_customer_id = c.source_customer_id
                LEFT JOIN sk_account a
                    ON ra.source_system = a.source_system
                   AND ra.source_account_id = a.source_account_id
                WHERE ra.is_deleted = FALSE
                  AND ra.source_system = %s
                  AND (ra.end_date IS NULL OR ra.end_date::date >= CURRENT_DATE)
                ORDER BY ra.source_account_id ASC, ra.day_of_week ASC, ra.sequence ASC NULLS LAST
                """,
                (source_system,),
            )
            rows = [dict(r) for r in cur.fetchall()]

    # Build tech name map for stable ordering
    tech_names: Dict[str, str] = {}
    for r in rows:
        tid = r["source_account_id"]
        if tid not in tech_names:
            tech_names[tid] = r.get("tech_name") or tid

    # Group by tech + day
    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = f"{row['source_account_id']}|{row['day_of_week']}"
        if key not in groups:
            groups[key] = {
                "source_account_id": row["source_account_id"],
                "tech_name": row.get("tech_name") or row["source_account_id"],
                "day_of_week": row["day_of_week"],
                "stops": [],
            }
        groups[key]["stops"].append({
            "source_route_assignment_id": row["source_route_assignment_id"],
            "source_service_location_id": row["source_service_location_id"],
            "source_customer_id": row.get("source_customer_id"),
            "source_account_id": row["source_account_id"],
            "tech_name": row.get("tech_name") or row["source_account_id"],
            "day_of_week": row["day_of_week"],
            "stop_order": row.get("stop_order"),
            "frequency": row.get("frequency"),
            "estimated_duration_minutes": row.get("estimated_duration_minutes") or 45,
            "latitude": float(row["latitude"]) if row.get("latitude") is not None else None,
            "longitude": float(row["longitude"]) if row.get("longitude") is not None else None,
            "address": row.get("address"),
            "city": row.get("city"),
            "customer_name": row.get("customer_name"),
            "rate_type": row.get("rate_type"),
            "location_notes": row.get("location_notes"),
        })

    result_groups = []
    for g in sorted(groups.values(), key=lambda x: (x["tech_name"], DAY_ORDER.get(x["day_of_week"], 99))):
        stops = sorted(g["stops"], key=lambda s: s.get("stop_order") or 9999)
        metrics = _build_route_metrics(stops)
        warnings = _build_route_warnings(stops)
        result_groups.append({**g, "stops": stops, "metrics": metrics, "warnings": warnings})

    all_stops = rows
    techs = sorted(
        [{"source_account_id": k, "tech_name": v} for k, v in tech_names.items()],
        key=lambda x: x["tech_name"],
    )

    return {
        "ok": True,
        "source": "skimmer_import",
        "as_of": date.today().isoformat(),
        "total_pools": len(all_stops),
        "technicians": techs,
        "route_groups": result_groups,
    }


# ── Technician route profiles ──────────────────────────────────────────────────

def list_technician_profiles(include_private: bool = False) -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.id,
                    p.technician_id,
                    p.display_name,
                    p.home_latitude,
                    p.home_longitude,
                    p.default_start_location_type,
                    p.default_end_location_type,
                    p.custom_start_latitude,
                    p.custom_start_longitude,
                    p.custom_end_latitude,
                    p.custom_end_longitude,
                    p.include_start_drive_in_metrics,
                    p.include_end_drive_in_metrics,
                    p.is_active,
                    p.notes,
                    p.created_at,
                    p.updated_at,
                    CASE WHEN %s THEN p.home_address ELSE NULL END AS home_address,
                    CASE WHEN %s THEN p.custom_start_address ELSE NULL END AS custom_start_address,
                    CASE WHEN %s THEN p.custom_end_address ELSE NULL END AS custom_end_address,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', a.first_name, a.last_name)), ''),
                        a.username,
                        p.technician_id
                    ) AS skimmer_tech_name
                FROM technician_route_profiles p
                LEFT JOIN sk_account a
                    ON a.source_system = 'skimmer'
                   AND a.source_account_id = p.technician_id
                ORDER BY skimmer_tech_name ASC
                """,
                (include_private, include_private, include_private),
            )
            items = [dict(r) for r in cur.fetchall()]
    return {"ok": True, "items": items, "total": len(items)}


def upsert_technician_profile(technician_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_postgres()
    allowed = {
        "display_name", "home_address", "home_latitude", "home_longitude",
        "default_start_location_type", "default_end_location_type",
        "custom_start_address", "custom_start_latitude", "custom_start_longitude",
        "custom_end_address", "custom_end_latitude", "custom_end_longitude",
        "include_start_drive_in_metrics", "include_end_drive_in_metrics",
        "is_active", "notes",
    }
    valid_start_end = {"home", "office", "first_stop", "last_stop", "custom"}
    data = {k: v for k, v in payload.items() if k in allowed}
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields provided")
    for key in ("default_start_location_type", "default_end_location_type"):
        if key in data and data[key] not in valid_start_end:
            raise HTTPException(status_code=400, detail=f"{key} must be one of: {', '.join(valid_start_end)}")

    set_clauses = [f"{k} = %s" for k in data]
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO technician_route_profiles (technician_id, {', '.join(data.keys())})
                VALUES (%s, {', '.join(['%s'] * len(data))})
                ON CONFLICT (technician_id) DO UPDATE
                SET {', '.join(set_clauses)}, updated_at = NOW()
                RETURNING *
                """,
                [technician_id] + list(data.values()) + list(data.values()),
            )
            row = cur.fetchone()
        conn.commit()
    return {"ok": True, "profile": dict(row) if row else {}}


def get_technician_profile(technician_id: str) -> Optional[Dict[str, Any]]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM technician_route_profiles WHERE technician_id = %s",
                (technician_id,),
            )
            row = cur.fetchone()
    return dict(row) if row else None


# ── Scenarios ─────────────────────────────────────────────────────────────────

def list_scenarios() -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.*,
                    COUNT(a.id) AS assignment_count
                FROM route_scenarios s
                LEFT JOIN route_scenario_assignments a ON a.scenario_id = s.id
                WHERE s.status <> 'archived'
                GROUP BY s.id
                ORDER BY s.updated_at DESC
                """
            )
            items = [dict(r) for r in cur.fetchall()]
    return {"ok": True, "items": items, "total": len(items)}


def create_scenario(name: str, notes: str = "", created_by: str = "") -> Dict[str, Any]:
    _require_postgres()
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO route_scenarios (name, notes, created_by)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (name.strip(), notes.strip() or None, created_by.strip() or None),
            )
            row = cur.fetchone()
        conn.commit()
    return {"ok": True, "scenario": dict(row)}


def get_scenario(scenario_id: int) -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM route_scenarios WHERE id = %s", (scenario_id,))
            scenario_row = cur.fetchone()
            if not scenario_row:
                raise HTTPException(status_code=404, detail="Scenario not found")
            cur.execute(
                """
                SELECT * FROM route_scenario_assignments
                WHERE scenario_id = %s
                ORDER BY source_account_id ASC, day_of_week ASC, stop_order ASC NULLS LAST
                """,
                (scenario_id,),
            )
            assignments = [dict(r) for r in cur.fetchall()]
    scenario = dict(scenario_row)
    # Group by tech + day
    groups: Dict[str, Dict[str, Any]] = {}
    for a in assignments:
        key = f"{a['source_account_id']}|{a['day_of_week']}"
        if key not in groups:
            groups[key] = {
                "source_account_id": a["source_account_id"],
                "tech_name": a.get("tech_name") or a["source_account_id"],
                "day_of_week": a["day_of_week"],
                "stops": [],
            }
        groups[key]["stops"].append({**a})

    route_groups = []
    for g in sorted(groups.values(), key=lambda x: (x["tech_name"], DAY_ORDER.get(x["day_of_week"], 99))):
        stops = sorted(g["stops"], key=lambda s: s.get("stop_order") or 9999)
        profile = get_technician_profile(g["source_account_id"])
        metrics = _build_route_metrics(stops, profile)
        warnings = _build_route_warnings(stops, profile)
        route_groups.append({**g, "stops": stops, "metrics": metrics, "warnings": warnings})

    return {"ok": True, "scenario": scenario, "route_groups": route_groups, "total_assignments": len(assignments)}


def update_scenario(scenario_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    _require_postgres()
    allowed = {"name", "status", "notes"}
    data = {k: v for k, v in payload.items() if k in allowed and v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields provided")
    if "status" in data and data["status"] not in SCENARIO_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(sorted(SCENARIO_STATUSES))}")
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM route_scenarios WHERE id = %s", (scenario_id,))
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Scenario not found")
            existing = dict(existing)
            current_status = existing.get("status")
            next_status = data.get("status")
            if next_status and next_status != current_status:
                allowed_next = SCENARIO_STATUS_TRANSITIONS.get(current_status, set())
                if next_status not in allowed_next:
                    raise HTTPException(status_code=409, detail=f"Invalid scenario status transition: {current_status} -> {next_status}")
                if next_status == "approved":
                    validation = validate_scenario(scenario_id)
                    if not validation.get("valid"):
                        raise HTTPException(status_code=409, detail="Scenario has unresolved validation errors")
            if any(k in data for k in ("name", "notes")) and current_status not in SCENARIO_EDITABLE_STATUSES:
                raise HTTPException(status_code=409, detail="Scenario details are read-only unless the scenario is a draft")
            set_clauses = [f"{k} = %s" for k in data] + ["updated_at = NOW()"]
            cur.execute(
                f"UPDATE route_scenarios SET {', '.join(set_clauses)} WHERE id = %s RETURNING *",
                list(data.values()) + [scenario_id],
            )
            row = cur.fetchone()
            if any(k in data for k in ("name", "notes")):
                _mark_change_plans_stale(cur, scenario_id)
        conn.commit()
    return {"ok": True, "scenario": dict(row)}


def create_scenario_from_current(name: str, notes: str = "", created_by: str = "", source_system: str = "skimmer") -> Dict[str, Any]:
    """Snapshot the current Skimmer route data into a new sandbox scenario."""
    _require_postgres()
    source_system = _validate_source_system(source_system)
    current = list_current_route_pools(source_system)
    scenario_result = create_scenario(name or f"Snapshot {date.today().isoformat()}", notes, created_by)
    scenario_id = scenario_result["scenario"]["id"]

    all_stops: List[Dict[str, Any]] = []
    for group in current["route_groups"]:
        all_stops.extend(group["stops"])

    with pg() as conn:
        with conn.cursor() as cur:
            for stop in all_stops:
                cur.execute(
                    """
                    INSERT INTO route_scenario_assignments (
                        scenario_id,
                        source_route_assignment_id,
                        source_service_location_id,
                        source_customer_id,
                        source_account_id,
                        day_of_week,
                        stop_order,
                        frequency,
                        estimated_duration_minutes,
                        latitude,
                        longitude,
                        address,
                        city,
                        customer_name,
                        tech_name,
                        rate_type
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        scenario_id,
                        stop.get("source_route_assignment_id"),
                        stop["source_service_location_id"],
                        stop.get("source_customer_id"),
                        stop["source_account_id"],
                        stop["day_of_week"],
                        stop.get("stop_order"),
                        stop.get("frequency"),
                        stop.get("estimated_duration_minutes"),
                        stop.get("latitude"),
                        stop.get("longitude"),
                        stop.get("address"),
                        stop.get("city"),
                        stop.get("customer_name"),
                        stop.get("tech_name"),
                        stop.get("rate_type"),
                    ),
                )
            cur.execute(
                "UPDATE route_scenarios SET source_snapshot_date = %s, updated_at = NOW() WHERE id = %s",
                (date.today().isoformat(), scenario_id),
            )
        conn.commit()

    return {
        "ok": True,
        "scenario_id": scenario_id,
        "assignments_created": len(all_stops),
        "scenario": scenario_result["scenario"],
    }


def duplicate_scenario(scenario_id: int, new_name: str = "", created_by: str = "") -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM route_scenarios WHERE id = %s", (scenario_id,))
            src = cur.fetchone()
            if not src:
                raise HTTPException(status_code=404, detail="Scenario not found")
            src = dict(src)
            cur.execute(
                """
                INSERT INTO route_scenarios (name, status, source_snapshot_date, created_by, notes)
                VALUES (%s, 'draft', %s, %s, %s)
                RETURNING *
                """,
                (
                    new_name.strip() or f"{src['name']} (copy)",
                    src.get("source_snapshot_date"),
                    created_by.strip() or src.get("created_by"),
                    src.get("notes"),
                ),
            )
            new_row = cur.fetchone()
            new_id = new_row["id"]
            cur.execute(
                """
                INSERT INTO route_scenario_assignments (
                    scenario_id, source_route_assignment_id, source_service_location_id,
                    source_customer_id, source_account_id, day_of_week, stop_order, frequency,
                    estimated_duration_minutes, latitude, longitude, address, city,
                    customer_name, tech_name, rate_type, notes, is_changed_from_current
                )
                SELECT
                    %s, source_route_assignment_id, source_service_location_id,
                    source_customer_id, source_account_id, day_of_week, stop_order, frequency,
                    estimated_duration_minutes, latitude, longitude, address, city,
                    customer_name, tech_name, rate_type, notes, is_changed_from_current
                FROM route_scenario_assignments
                WHERE scenario_id = %s
                """,
                (new_id, scenario_id),
            )
        conn.commit()
    return {"ok": True, "scenario": dict(new_row)}


def discard_scenario(scenario_id: int) -> Dict[str, Any]:
    return update_scenario(scenario_id, {"status": "archived"})


# ── Assignment editing ─────────────────────────────────────────────────────────

def move_assignment(
    scenario_id: int,
    assignment_id: int,
    new_account_id: str,
    new_day_of_week: str,
    new_stop_order: Optional[int] = None,
) -> Dict[str, Any]:
    _require_postgres()
    new_day_of_week = _require_valid_day(new_day_of_week)
    if not str(new_account_id or "").strip():
        raise HTTPException(status_code=400, detail="source_account_id is required")
    try:
        new_stop_order = int(new_stop_order) if new_stop_order is not None else None
    except Exception:
        raise HTTPException(status_code=400, detail="stop_order must be an integer")
    with pg() as conn:
        with conn.cursor() as cur:
            _ensure_scenario_editable(cur, scenario_id)
            cur.execute(
                "SELECT * FROM route_scenario_assignments WHERE id = %s AND scenario_id = %s",
                (assignment_id, scenario_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Assignment not found in scenario")
            # Find new tech name
            cur.execute(
                "SELECT first_name, last_name, username FROM sk_account WHERE source_system = 'skimmer' AND source_account_id = %s",
                (new_account_id,),
            )
            tech_row = cur.fetchone()
            if not tech_row:
                raise HTTPException(status_code=400, detail="source_account_id does not match a known Skimmer technician")
            tech_name = None
            if tech_row:
                tech_name = (
                    " ".join(filter(None, [tech_row.get("first_name"), tech_row.get("last_name")])).strip()
                    or tech_row.get("username")
                    or new_account_id
                )
            # If no explicit stop order, place at end of target group
            if new_stop_order is None:
                cur.execute(
                    """
                    SELECT COALESCE(MAX(stop_order), 0) + 10 AS next_order
                    FROM route_scenario_assignments
                    WHERE scenario_id = %s AND source_account_id = %s AND day_of_week = %s AND id <> %s
                    """,
                    (scenario_id, new_account_id, new_day_of_week, assignment_id),
                )
                order_row = cur.fetchone()
                new_stop_order = int(order_row["next_order"]) if order_row else 10
            cur.execute(
                """
                UPDATE route_scenario_assignments
                SET source_account_id = %s,
                    day_of_week = %s,
                    stop_order = %s,
                    tech_name = %s,
                    is_changed_from_current = TRUE,
                    updated_at = NOW()
                WHERE id = %s AND scenario_id = %s
                RETURNING *
                """,
                (new_account_id, new_day_of_week, new_stop_order, tech_name, assignment_id, scenario_id),
            )
            updated = cur.fetchone()
            cur.execute(
                "UPDATE route_scenarios SET updated_at = NOW() WHERE id = %s",
                (scenario_id,),
            )
            _mark_change_plans_stale(cur, scenario_id)
            _mark_route_estimates_stale(cur, scenario_id)
        conn.commit()
    return {"ok": True, "assignment": dict(updated) if updated else {}}


def reorder_assignments(scenario_id: int, ordered_ids: List[int]) -> Dict[str, Any]:
    """Reassign stop_order based on the provided ordered list of assignment IDs."""
    _require_postgres()
    if not ordered_ids:
        raise HTTPException(status_code=400, detail="ordered_ids must not be empty")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise HTTPException(status_code=400, detail="ordered_ids must not contain duplicates")
    with pg() as conn:
        with conn.cursor() as cur:
            _ensure_scenario_editable(cur, scenario_id)
            cur.execute(
                """
                SELECT id, source_account_id, day_of_week
                FROM route_scenario_assignments
                WHERE scenario_id = %s AND id = ANY(%s)
                """,
                (scenario_id, ordered_ids),
            )
            found_rows = [dict(r) for r in cur.fetchall()]
            found_ids = {r["id"] for r in found_rows}
            missing = [i for i in ordered_ids if i not in found_ids]
            if missing:
                raise HTTPException(status_code=404, detail=f"Assignment IDs not found in scenario: {missing}")
            group_keys = {(r["source_account_id"], r["day_of_week"]) for r in found_rows}
            if len(group_keys) != 1:
                raise HTTPException(status_code=400, detail="Reorder can only operate within one technician/day route group")
            for order, aid in enumerate(ordered_ids, start=1):
                cur.execute(
                    """
                    UPDATE route_scenario_assignments
                    SET stop_order = %s, is_changed_from_current = TRUE, updated_at = NOW()
                    WHERE id = %s AND scenario_id = %s
                    """,
                    (order * 10, aid, scenario_id),
                )
            cur.execute("UPDATE route_scenarios SET updated_at = NOW() WHERE id = %s", (scenario_id,))
            _mark_change_plans_stale(cur, scenario_id)
            tech_id, route_day = next(iter(group_keys))
            _mark_route_estimates_stale(cur, scenario_id, tech_id, route_day)
        conn.commit()
    return {"ok": True, "reordered": len(ordered_ids)}


# ── Validation ────────────────────────────────────────────────────────────────

def validate_scenario(scenario_id: int) -> Dict[str, Any]:
    _require_postgres()
    detail = get_scenario(scenario_id)
    scenario = detail["scenario"]
    route_groups = detail["route_groups"]

    all_warnings: List[Dict[str, Any]] = []
    all_errors: List[Dict[str, Any]] = []
    seen_locations: Dict[str, List[str]] = {}
    valid_days = set(DAY_ORDER.keys())

    for group in route_groups:
        key = f"{group['tech_name']} / {group['day_of_week']}"
        if not group.get("source_account_id"):
            all_errors.append({"group": key, "message": "Route group is missing a technician."})
        if group.get("day_of_week") not in valid_days:
            all_errors.append({"group": key, "message": f"Route group has invalid day: {group.get('day_of_week') or 'blank'}."})
        for w in group.get("warnings", []):
            all_warnings.append({"group": key, "message": w})
        for stop in group["stops"]:
            loc = stop.get("source_service_location_id", "")
            if not loc:
                all_errors.append({"group": key, "message": "Assignment is missing a service location id."})
            if not stop.get("source_account_id"):
                all_errors.append({"group": key, "message": f"{stop.get('customer_name') or loc or 'Assignment'} is missing a technician."})
            if stop.get("day_of_week") not in valid_days:
                all_errors.append({"group": key, "message": f"{stop.get('customer_name') or loc or 'Assignment'} has invalid day: {stop.get('day_of_week') or 'blank'}."})
            seen_locations.setdefault(loc, []).append(key)

    # Duplicate location across route groups
    for loc, groups in seen_locations.items():
        if loc and len(groups) > 1:
            all_errors.append({
                "group": "global",
                "message": f"Service location {loc} appears in multiple route groups: {', '.join(groups)}",
            })

    return {
        "ok": True,
        "scenario_id": scenario_id,
        "scenario_name": scenario.get("name"),
        "error_count": len(all_errors),
        "warning_count": len(all_warnings),
        "errors": all_errors,
        "warnings": all_warnings,
        "valid": len(all_errors) == 0,
    }


def _load_scenario_for_plan(cur, scenario_id: int) -> Dict[str, Any]:
    cur.execute("SELECT * FROM route_scenarios WHERE id = %s", (scenario_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return dict(row)


def _require_approved_valid_scenario_for_packet(scenario_id: int) -> Dict[str, Any]:
    validation = validate_scenario(scenario_id)
    with pg() as conn:
        with conn.cursor() as cur:
            scenario = _load_scenario_for_plan(cur, scenario_id)
    if scenario.get("status") != "approved":
        raise HTTPException(status_code=409, detail="Manual update packets can only be generated from approved scenarios")
    if not validation.get("valid"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Scenario has unresolved validation errors",
                "errors": validation.get("errors", []),
            },
        )
    return scenario


# ── Comparison (scenario vs current) ─────────────────────────────────────────

def get_comparison(scenario_id: int, source_system: str = "skimmer") -> Dict[str, Any]:
    _require_postgres()
    source_system = _validate_source_system(source_system)
    current = list_current_route_pools(source_system)
    scenario_detail = get_scenario(scenario_id)

    # Build current lookup by location_id → (account_id, day, order)
    current_map: Dict[str, Dict[str, Any]] = {}
    for group in current["route_groups"]:
        for stop in group["stops"]:
            loc = stop["source_service_location_id"]
            current_map[loc] = {
                "source_route_assignment_id": stop.get("source_route_assignment_id"),
                "source_account_id": stop["source_account_id"],
                "tech_name": stop.get("tech_name"),
                "day_of_week": stop["day_of_week"],
                "stop_order": stop.get("stop_order"),
                "customer_name": stop.get("customer_name"),
                "address": stop.get("address"),
            }

    # Build scenario lookup
    scenario_map: Dict[str, Dict[str, Any]] = {}
    for group in scenario_detail["route_groups"]:
        for stop in group["stops"]:
            loc = stop["source_service_location_id"]
            scenario_map[loc] = {
                "source_account_id": stop["source_account_id"],
                "tech_name": stop.get("tech_name"),
                "day_of_week": stop["day_of_week"],
                "stop_order": stop.get("stop_order"),
                "customer_name": stop.get("customer_name"),
                "address": stop.get("address"),
                "assignment_id": stop.get("id"),
            }

    all_locations = set(current_map.keys()) | set(scenario_map.keys())
    changes: List[Dict[str, Any]] = []
    added: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    reordered: List[Dict[str, Any]] = []
    unchanged_count = 0

    for loc in sorted(all_locations):
        cur = current_map.get(loc)
        prp = scenario_map.get(loc)
        if cur is None and prp is not None:
            added.append({**prp, "source_service_location_id": loc, "change_type": "added"})
        elif cur is not None and prp is None:
            removed.append({**cur, "source_service_location_id": loc, "change_type": "removed"})
        elif cur and prp:
            tech_changed = cur["source_account_id"] != prp["source_account_id"]
            day_changed = cur["day_of_week"] != prp["day_of_week"]
            order_changed = cur["stop_order"] != prp["stop_order"]
            if tech_changed or day_changed:
                changes.append({
                    "source_service_location_id": loc,
                    "customer_name": cur.get("customer_name") or prp.get("customer_name"),
                    "address": cur.get("address") or prp.get("address"),
                    "change_type": "moved",
                    "current_account_id": cur["source_account_id"],
                    "current_tech_name": cur.get("tech_name"),
                    "current_day": cur["day_of_week"],
                    "current_order": cur.get("stop_order"),
                    "proposed_account_id": prp["source_account_id"],
                    "proposed_tech_name": prp.get("tech_name"),
                    "proposed_day": prp["day_of_week"],
                    "proposed_order": prp.get("stop_order"),
                    "assignment_id": prp.get("assignment_id"),
                })
            elif order_changed:
                reordered.append({
                    "source_service_location_id": loc,
                    "customer_name": cur.get("customer_name"),
                    "address": cur.get("address"),
                    "change_type": "reordered",
                    "source_account_id": cur["source_account_id"],
                    "tech_name": cur.get("tech_name"),
                    "day_of_week": cur["day_of_week"],
                    "current_order": cur.get("stop_order"),
                    "proposed_order": prp.get("stop_order"),
                })
            else:
                unchanged_count += 1

    return {
        "ok": True,
        "scenario_id": scenario_id,
        "scenario_name": scenario_detail["scenario"].get("name"),
        "total_current": len(current_map),
        "total_proposed": len(scenario_map),
        "moved_count": len(changes),
        "added_count": len(added),
        "removed_count": len(removed),
        "reordered_count": len(reordered),
        "unchanged_count": unchanged_count,
        "changes": changes,
        "added": added,
        "removed": removed,
        "reordered": reordered,
        "current_route_groups": current["route_groups"],
        "proposed_route_groups": scenario_detail["route_groups"],
    }


# ── Change plan ────────────────────────────────────────────────────────────────

def generate_change_plan(scenario_id: int) -> Dict[str, Any]:
    _require_postgres()
    scenario = _require_approved_valid_scenario_for_packet(scenario_id)
    comparison = get_comparison(scenario_id)

    items: List[Dict[str, Any]] = []
    for c in comparison["changes"]:
        items.append({
            "change_type": "update_assignment",
            "source_service_location_id": c["source_service_location_id"],
            "customer_name": c.get("customer_name"),
            "address": c.get("address"),
            "source_account_id_current": c["current_account_id"],
            "day_of_week_current": c["current_day"],
            "stop_order_current": c.get("current_order"),
            "source_account_id_proposed": c["proposed_account_id"],
            "day_of_week_proposed": c["proposed_day"],
            "stop_order_proposed": c.get("proposed_order"),
            "skimmer_route_assignment_id": None,
        })
    for c in comparison["added"]:
        items.append({
            "change_type": "create_assignment",
            "source_service_location_id": c["source_service_location_id"],
            "customer_name": c.get("customer_name"),
            "address": c.get("address"),
            "source_account_id_current": None,
            "day_of_week_current": None,
            "stop_order_current": None,
            "source_account_id_proposed": c["source_account_id"],
            "day_of_week_proposed": c["day_of_week"],
            "stop_order_proposed": c.get("stop_order"),
            "skimmer_route_assignment_id": None,
        })
    for c in comparison["removed"]:
        items.append({
            "change_type": "delete_assignment",
            "source_service_location_id": c["source_service_location_id"],
            "customer_name": c.get("customer_name"),
            "address": c.get("address"),
            "source_account_id_current": c["source_account_id"],
            "day_of_week_current": c["day_of_week"],
            "stop_order_current": c.get("stop_order"),
            "source_account_id_proposed": None,
            "day_of_week_proposed": None,
            "stop_order_proposed": None,
            "skimmer_route_assignment_id": None,
        })
    for c in comparison["reordered"]:
        items.append({
            "change_type": "reorder_stop",
            "source_service_location_id": c["source_service_location_id"],
            "customer_name": c.get("customer_name"),
            "address": c.get("address"),
            "source_account_id_current": c.get("source_account_id"),
            "day_of_week_current": c.get("day_of_week"),
            "stop_order_current": c.get("current_order"),
            "source_account_id_proposed": c.get("source_account_id"),
            "day_of_week_proposed": c.get("day_of_week"),
            "stop_order_proposed": c.get("proposed_order"),
            "skimmer_route_assignment_id": None,
        })

    summary = {
        "moved_count": comparison["moved_count"],
        "added_count": comparison["added_count"],
        "removed_count": comparison["removed_count"],
        "reordered_count": comparison["reordered_count"],
        "unchanged_count": comparison["unchanged_count"],
        "total_changes": len(items),
    }

    items.sort(
        key=lambda item: (
            item.get("day_of_week_proposed") or item.get("day_of_week_current") or "",
            item.get("source_account_id_proposed") or item.get("source_account_id_current") or "",
            item.get("stop_order_proposed") or item.get("stop_order_current") or 0,
            item.get("source_service_location_id") or "",
            item.get("change_type") or "",
        )
    )

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE route_change_plans
                SET status = 'stale',
                    error_json = jsonb_build_object('reason', 'New manual update packet generated'),
                    updated_at = NOW()
                WHERE scenario_id = %s
                  AND status IN ('generated', 'approved', 'printed')
                """,
                (scenario_id,),
            )
            cur.execute(
                """
                INSERT INTO route_change_plans (scenario_id, scenario_updated_at, summary_json)
                VALUES (%s, %s, %s::jsonb)
                RETURNING *
                """,
                (scenario_id, scenario.get("updated_at"), json.dumps(summary)),
            )
            plan_row = cur.fetchone()
            plan_id = plan_row["id"]
            persisted_items: List[Dict[str, Any]] = []
            for item in items:
                cur.execute(
                    """
                    INSERT INTO route_change_plan_items (
                        change_plan_id, change_type,
                        source_service_location_id, customer_name, address,
                        source_account_id_current, day_of_week_current, stop_order_current,
                        source_account_id_proposed, day_of_week_proposed, stop_order_proposed,
                        skimmer_route_assignment_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        plan_id,
                        item["change_type"],
                        item.get("source_service_location_id"),
                        item.get("customer_name"),
                        item.get("address"),
                        item.get("source_account_id_current"),
                        item.get("day_of_week_current"),
                        item.get("stop_order_current"),
                        item.get("source_account_id_proposed"),
                        item.get("day_of_week_proposed"),
                        item.get("stop_order_proposed"),
                        item.get("skimmer_route_assignment_id"),
                    ),
                )
                inserted = cur.fetchone()
                if inserted:
                    persisted_items.append(dict(inserted))
        conn.commit()

    return {"ok": True, "change_plan_id": plan_id, "change_plan": dict(plan_row), "summary": summary, "items": persisted_items}


def get_change_plan(plan_id: int) -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM route_change_plans WHERE id = %s", (plan_id,))
            plan = cur.fetchone()
            if not plan:
                raise HTTPException(status_code=404, detail="Change plan not found")
            cur.execute(
                "SELECT * FROM route_change_plan_items WHERE change_plan_id = %s ORDER BY id",
                (plan_id,),
            )
            items = [dict(r) for r in cur.fetchall()]
    return {"ok": True, "change_plan": dict(plan), "items": items}


def approve_change_plan(plan_id: int, approved_by: str = "") -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.*, s.updated_at AS current_scenario_updated_at
                FROM route_change_plans p
                JOIN route_scenarios s ON s.id = p.scenario_id
                WHERE p.id = %s
                """,
                (plan_id,),
            )
            existing = cur.fetchone()
            if not existing:
                raise HTTPException(status_code=404, detail="Change plan not found")
            existing_plan = dict(existing)
            if existing_plan.get("status") != "generated":
                raise HTTPException(status_code=409, detail="Change plan is not in generated status")
            if existing_plan.get("scenario_updated_at") != existing_plan.get("current_scenario_updated_at"):
                raise HTTPException(status_code=409, detail="Change plan is stale; regenerate the manual update packet")
            validation = validate_scenario(int(existing_plan["scenario_id"]))
            if not validation.get("valid"):
                raise HTTPException(status_code=409, detail="Scenario has unresolved validation errors")
            cur.execute(
                """
                UPDATE route_change_plans
                SET status = 'approved', approved_by = %s, approved_at = NOW(), updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (approved_by.strip() or "user", plan_id),
            )
            row = cur.fetchone()
        conn.commit()
    return {"ok": True, "change_plan": dict(row)}


def mark_plan_item(plan_id: int, item_id: int, status: str, notes: str = "") -> Dict[str, Any]:
    """Mark a manual update packet item as completed/skipped/needs_review after human applies it in Skimmer."""
    _require_postgres()
    valid = {"pending", "completed", "skipped", "needs_review"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(valid)}")
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM route_change_plans WHERE id = %s", (plan_id,))
            plan_row = cur.fetchone()
            if not plan_row:
                raise HTTPException(status_code=404, detail="Change plan not found")
            plan = dict(plan_row)
            if _route_change_plan_status(plan.get("status")) not in CHANGE_PLAN_ACTIONABLE_STATUSES:
                raise HTTPException(status_code=409, detail="Manual checklist items can only be marked after the packet is approved")
            cur.execute(
                """
                UPDATE route_change_plan_items
                SET status = %s, error_message = %s
                WHERE id = %s AND change_plan_id = %s
                RETURNING *
                """,
                (status, notes.strip() or None, item_id, plan_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Plan item not found")
            # Update plan status when all items are resolved
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status = 'needs_review') AS needs_review
                FROM route_change_plan_items
                WHERE change_plan_id = %s
                """,
                (plan_id,),
            )
            counts = dict(cur.fetchone())
            if counts["pending"] == 0 and counts["needs_review"] == 0:
                cur.execute(
                    """
                    UPDATE route_change_plans
                    SET status = 'manually_completed',
                        manually_completed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s AND status IN ('approved', 'printed', 'manually_in_progress')
                    RETURNING scenario_id
                    """,
                    (plan_id,),
                )
                completed_plan = cur.fetchone()
                if completed_plan:
                    cur.execute(
                        """
                        UPDATE route_scenarios
                        SET status = 'manually_completed', updated_at = NOW()
                        WHERE id = %s AND status = 'approved'
                        """,
                        (completed_plan["scenario_id"],),
                    )
            elif counts["completed"] > 0:
                cur.execute(
                    """
                    UPDATE route_change_plans
                    SET status = 'manually_in_progress', updated_at = NOW()
                    WHERE id = %s AND status IN ('approved', 'printed')
                    """,
                    (plan_id,),
                )
        conn.commit()
    return {"ok": True, "item": dict(row)}


def mark_plan_printed(plan_id: int) -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE route_change_plans
                SET status = 'printed', updated_at = NOW()
                WHERE id = %s AND status = 'approved'
                RETURNING *
                """,
                (plan_id,),
            )
            row = cur.fetchone()
            if not row:
                cur.execute("SELECT * FROM route_change_plans WHERE id = %s", (plan_id,))
                row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Change plan not found")
        conn.commit()
    return {"ok": True, "change_plan": dict(row)}


def export_change_plan_csv(plan_id: int) -> str:
    """Return CSV text of the change plan items for manual Skimmer update."""
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT s.name AS scenario_name FROM route_change_plans p JOIN route_scenarios s ON s.id = p.scenario_id WHERE p.id = %s", (plan_id,))
            plan_meta = cur.fetchone()
            scenario_name = dict(plan_meta).get("scenario_name", "") if plan_meta else ""
            cur.execute("SELECT * FROM route_change_plan_items WHERE change_plan_id = %s ORDER BY id", (plan_id,))
            items = [dict(r) for r in cur.fetchall()]

    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "scenario_name", "change_type",
        "customer_name", "service_address",
        "current_tech_id", "current_day", "current_stop_order",
        "proposed_tech_id", "proposed_day", "proposed_stop_order",
        "skimmer_route_assignment_id", "item_status", "notes",
        "manual_update_completed",
    ])
    for item in items:
        writer.writerow([
            scenario_name,
            item.get("change_type", ""),
            item.get("customer_name", ""),
            item.get("address", ""),
            item.get("source_account_id_current", ""),
            item.get("day_of_week_current", ""),
            item.get("stop_order_current", ""),
            item.get("source_account_id_proposed", ""),
            item.get("day_of_week_proposed", ""),
            item.get("stop_order_proposed", ""),
            item.get("skimmer_route_assignment_id", ""),
            item.get("status", "pending"),
            item.get("error_message", ""),
            "YES" if item.get("status") == "completed" else "NO",
        ])
    return buf.getvalue()


def ensure_schema() -> None:
    ensure_route_sandbox_schema()


# ── Google Maps integration ───────────────────────────────────────────────────

def get_maps_status() -> Dict[str, Any]:
    from services import google_maps
    return {
        "server_maps_configured": google_maps.server_configured(),
        "browser_maps_configured": google_maps.browser_configured(),
        "optimization_enabled": google_maps.server_configured() and google_maps.optimization_enabled(),
        "daily_request_limit": _google_maps_daily_request_limit(),
        "daily_matrix_element_limit": _google_maps_daily_matrix_element_limit(),
        "cache_ttl_days": google_maps.cache_ttl_days(),
    }


def _check_and_record_google_maps_usage(cur, request_count: int = 1, matrix_element_count: int = 0) -> None:
    """Enforce Sentinel app-level Google Maps limits before making paid calls."""
    request_limit = _google_maps_daily_request_limit()
    matrix_limit = _google_maps_daily_matrix_element_limit()
    if request_limit <= 0:
        raise HTTPException(status_code=429, detail="Google Maps requests are disabled by Sentinel daily limit")
    today = date.today()
    cur.execute(
        """
        INSERT INTO route_google_usage_daily (usage_date, request_count, matrix_element_count)
        VALUES (%s, 0, 0)
        ON CONFLICT (usage_date) DO NOTHING
        """,
        (today,),
    )
    cur.execute(
        """
        SELECT request_count, matrix_element_count
        FROM route_google_usage_daily
        WHERE usage_date = %s
        FOR UPDATE
        """,
        (today,),
    )
    row = cur.fetchone() or {"request_count": 0, "matrix_element_count": 0}
    next_requests = int(row["request_count"] or 0) + int(request_count or 0)
    next_matrix = int(row["matrix_element_count"] or 0) + int(matrix_element_count or 0)
    if next_requests > request_limit:
        raise HTTPException(status_code=429, detail="Google Maps daily request limit reached for Sentinel")
    if matrix_limit > 0 and next_matrix > matrix_limit:
        raise HTTPException(status_code=429, detail="Google Maps daily route-matrix element limit reached for Sentinel")
    cur.execute(
        """
        UPDATE route_google_usage_daily
        SET request_count = %s,
            matrix_element_count = %s,
            updated_at = NOW()
        WHERE usage_date = %s
        """,
        (next_requests, next_matrix, today),
    )


def _get_cached_segment(cur, origin_hash: str, dest_hash: str, mode: str = "DRIVE") -> Optional[Dict]:
    cur.execute(
        """
        SELECT distance_meters, duration_seconds, polyline, provider
        FROM route_distance_cache
        WHERE origin_hash = %s AND destination_hash = %s AND travel_mode = %s
          AND expires_at > NOW()
        LIMIT 1
        """,
        (origin_hash, dest_hash, mode),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _store_cached_segment(
    cur,
    origin_hash: str, dest_hash: str,
    origin_lat: float, origin_lng: float,
    dest_lat: float, dest_lng: float,
    distance_m: int, duration_s: int,
    polyline: str = "",
    mode: str = "DRIVE",
) -> None:
    from services import google_maps
    from datetime import timezone, timedelta
    expires = datetime.now(timezone.utc) + timedelta(days=google_maps.cache_ttl_days())
    cur.execute(
        """
        INSERT INTO route_distance_cache (
            origin_hash, destination_hash,
            origin_latitude, origin_longitude,
            destination_latitude, destination_longitude,
            travel_mode, distance_meters, duration_seconds,
            polyline, expires_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (origin_hash, destination_hash, travel_mode) DO UPDATE SET
            distance_meters = EXCLUDED.distance_meters,
            duration_seconds = EXCLUDED.duration_seconds,
            polyline = EXCLUDED.polyline,
            calculated_at = NOW(),
            expires_at = EXCLUDED.expires_at
        """,
        (origin_hash, dest_hash, origin_lat, origin_lng,
         dest_lat, dest_lng, mode, distance_m, duration_s,
         polyline, expires),
    )


def _stops_for_route(
    scenario_id: Optional[int],
    source_type: str,
    technician_id: str,
    day_of_week: str,
    source_system: str = "skimmer",
) -> List[Dict]:
    """Return ordered stops for a given tech/day from a scenario or current routes."""
    if source_type == "scenario" and scenario_id:
        detail = get_scenario(scenario_id)
        stops = [
            s
            for g in detail["route_groups"]
            for s in g["stops"]
            if g["source_account_id"] == technician_id and g["day_of_week"] == day_of_week
        ]
    else:
        current = list_current_route_pools(source_system)
        stops = [
            s
            for g in current["route_groups"]
            for s in g["stops"]
            if g["source_account_id"] == technician_id and g["day_of_week"] == day_of_week
        ]
    stops.sort(key=lambda x: (x.get("stop_order") or 9999))
    return stops


def calculate_route_estimate(
    technician_id: str,
    day_of_week: str,
    scenario_id: Optional[int] = None,
    source_type: str = "scenario",
    tech_profile: Optional[Dict] = None,
    force_refresh: bool = False,
    source_system: str = "skimmer",
) -> Dict[str, Any]:
    """
    Calculate true driving distance/time for a single tech/day route via Google Maps.
    Results are cached segment-by-segment in route_distance_cache.
    Persists a route_estimate_run record with segments.
    """
    from services import google_maps

    _require_postgres()
    if not google_maps.server_configured():
        raise HTTPException(status_code=400, detail="GOOGLE_MAPS_SERVER_API_KEY is not configured")
    if source_type not in {"scenario", "current"}:
        raise HTTPException(status_code=400, detail="source_type must be scenario or current")
    day_of_week = _require_valid_day(day_of_week)

    stops = _stops_for_route(scenario_id, source_type, technician_id, day_of_week, source_system)
    if not stops:
        raise HTTPException(status_code=404, detail="No stops found for this tech/day")

    # Determine optional start / end anchor locations from profile.
    start_lat, start_lng, start_type = _route_endpoint(tech_profile, "start")
    end_lat, end_lng, end_type = _route_endpoint(tech_profile, "end")
    start_lat = float(start_lat) if start_lat is not None else None
    start_lng = float(start_lng) if start_lng is not None else None
    end_lat = float(end_lat) if end_lat is not None else None
    end_lng = float(end_lng) if end_lng is not None else None

    # Build ordered sequence of points
    points: List[Dict] = []
    if start_lat is not None:
        points.append({"type": start_type, "lat": start_lat, "lng": start_lng, "pool_id": None, "duration_min": 0})
    for s in stops:
        lat = float(s["latitude"]) if s.get("latitude") is not None else None
        lng = float(s["longitude"]) if s.get("longitude") is not None else None
        points.append({
            "type": "pool",
            "lat": lat,
            "lng": lng,
            "pool_id": s.get("source_service_location_id"),
            "duration_min": s.get("estimated_duration_minutes") or 30,
        })
    if end_lat is not None:
        points.append({"type": end_type, "lat": end_lat, "lng": end_lng, "pool_id": None, "duration_min": 0})

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO route_estimate_runs
                    (scenario_id, source_type, technician_id, service_day, provider, status, stop_count)
                VALUES (%s, %s, %s, %s, 'google_maps', 'pending', %s) RETURNING id
                """,
                (scenario_id, source_type, technician_id, day_of_week, len(stops)),
            )
            run_id = cur.fetchone()["id"]
        conn.commit()

        warnings: List[str] = []
        request_count = 0
        cache_hit_count = 0
        seg_records: List[Dict] = []

        total_distance_m = 0
        total_duration_s = 0
        c2c_distance_m = 0
        c2c_duration_s = 0
        start_to_first_m = start_to_first_s = None
        last_to_end_m = last_to_end_s = None
        service_duration_s = sum(p["duration_min"] * 60 for p in points if p["type"] == "pool")

        pairs = list(zip(points, points[1:]))
        has_start_anchor = start_lat is not None
        has_end_anchor = end_lat is not None

        for seq, (pt_from, pt_to) in enumerate(pairs):
            seg: Dict[str, Any] = {
                "sequence": seq,
                "from_type": pt_from["type"],
                "from_pool_id": pt_from.get("pool_id"),
                "from_latitude": pt_from.get("lat"),
                "from_longitude": pt_from.get("lng"),
                "to_type": pt_to["type"],
                "to_pool_id": pt_to.get("pool_id"),
                "to_latitude": pt_to.get("lat"),
                "to_longitude": pt_to.get("lng"),
                "distance_meters": 0,
                "duration_seconds": 0,
                "polyline": "",
                "cache_hit": False,
                "error_message": None,
            }

            if pt_from.get("lat") is None or pt_from.get("lng") is None or pt_to.get("lat") is None or pt_to.get("lng") is None:
                seg["error_message"] = "Missing coordinates"
                warnings.append(f"Segment {seq}: missing coordinates — skipped")
            else:
                oh, dh = google_maps.segment_hashes(
                    pt_from["lat"], pt_from["lng"],
                    pt_to["lat"], pt_to["lng"],
                )
                with conn.cursor() as cur:
                    cached = None if force_refresh else _get_cached_segment(cur, oh, dh)
                if cached:
                    seg.update({
                        "distance_meters": cached["distance_meters"],
                        "duration_seconds": cached["duration_seconds"],
                        "polyline": cached.get("polyline") or "",
                        "cache_hit": True,
                    })
                    cache_hit_count += 1
                else:
                    with conn.cursor() as cur:
                        _check_and_record_google_maps_usage(cur, request_count=1, matrix_element_count=0)
                    conn.commit()
                    result = google_maps.compute_route(
                        pt_from["lat"], pt_from["lng"],
                        pt_to["lat"], pt_to["lng"],
                    )
                    request_count += 1
                    if result.get("error"):
                        seg["error_message"] = result["error"]
                        warnings.append(f"Segment {seq}: {result['error']}")
                    else:
                        seg.update({
                            "distance_meters": result["distance_meters"],
                            "duration_seconds": result["duration_seconds"],
                            "polyline": result.get("polyline", ""),
                        })
                        with conn.cursor() as cur:
                            _store_cached_segment(
                                cur, oh, dh,
                                pt_from["lat"], pt_from["lng"],
                                pt_to["lat"], pt_to["lng"],
                                seg["distance_meters"], seg["duration_seconds"],
                                seg["polyline"],
                            )
                        conn.commit()

            seg_records.append(seg)
            dm, ds = seg["distance_meters"], seg["duration_seconds"]
            total_distance_m += dm
            total_duration_s += ds

            is_first_seg = seq == 0
            is_last_seg = seq == len(pairs) - 1

            if has_start_anchor and is_first_seg:
                start_to_first_m, start_to_first_s = dm, ds
            elif has_end_anchor and is_last_seg:
                last_to_end_m, last_to_end_s = dm, ds
            else:
                c2c_distance_m += dm
                c2c_duration_s += ds

        total_work_s = total_duration_s + service_duration_s

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE route_estimate_runs SET
                    status = 'complete',
                    total_distance_meters = %s,
                    total_duration_seconds = %s,
                    customer_to_customer_distance_meters = %s,
                    customer_to_customer_duration_seconds = %s,
                    start_to_first_distance_meters = %s,
                    start_to_first_duration_seconds = %s,
                    last_to_end_distance_meters = %s,
                    last_to_end_duration_seconds = %s,
                    service_duration_seconds = %s,
                    total_work_duration_seconds = %s,
                    request_count = %s,
                    cache_hit_count = %s,
                    warnings = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    total_distance_m, total_duration_s,
                    c2c_distance_m, c2c_duration_s,
                    start_to_first_m, start_to_first_s,
                    last_to_end_m, last_to_end_s,
                    service_duration_s, total_work_s,
                    request_count, cache_hit_count,
                    json.dumps(warnings), run_id,
                ),
            )
            for seg in seg_records:
                cur.execute(
                    """
                    INSERT INTO route_estimate_segments (
                        estimate_run_id, sequence,
                        from_type, from_pool_id, from_latitude, from_longitude,
                        to_type, to_pool_id, to_latitude, to_longitude,
                        distance_meters, duration_seconds, polyline,
                        provider, cache_hit, error_message
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'google_maps',%s,%s)
                    """,
                    (
                        run_id, seg["sequence"],
                        seg["from_type"], seg["from_pool_id"],
                        seg["from_latitude"], seg["from_longitude"],
                        seg["to_type"], seg["to_pool_id"],
                        seg["to_latitude"], seg["to_longitude"],
                        seg["distance_meters"], seg["duration_seconds"],
                        seg["polyline"], seg["cache_hit"], seg["error_message"],
                    ),
                )
        conn.commit()

    def _mi(m: Optional[int]) -> Optional[float]:
        return round((m or 0) / 1609.34, 1) if m is not None else None

    def _min(s: Optional[int]) -> Optional[int]:
        return round((s or 0) / 60) if s is not None else None

    return {
        "ok": True,
        "run_id": run_id,
        "technician_id": technician_id,
        "day_of_week": day_of_week,
        "stop_count": len(stops),
        "total_distance_miles": _mi(total_distance_m),
        "total_duration_minutes": _min(total_duration_s),
        "customer_to_customer_distance_miles": _mi(c2c_distance_m),
        "customer_to_customer_duration_minutes": _min(c2c_duration_s),
        "start_to_first_distance_miles": _mi(start_to_first_m),
        "start_to_first_duration_minutes": _min(start_to_first_s),
        "last_to_end_distance_miles": _mi(last_to_end_m),
        "last_to_end_duration_minutes": _min(last_to_end_s),
        "service_duration_minutes": _min(service_duration_s),
        "total_work_duration_minutes": _min(total_work_s),
        "request_count": request_count,
        "cache_hit_count": cache_hit_count,
        "warnings": warnings,
        "segments": [
            {
                "sequence": s["sequence"],
                "from_type": s["from_type"],
                "to_type": s["to_type"],
                "distance_miles": round((s["distance_meters"] or 0) / 1609.34, 2),
                "duration_minutes": round((s["duration_seconds"] or 0) / 60, 1),
                "polyline": s["polyline"],
                "cache_hit": s["cache_hit"],
                "error": s.get("error_message"),
            }
            for s in seg_records
        ],
    }


def get_route_estimates(
    scenario_id: int,
    technician_id: Optional[str] = None,
    day_of_week: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the most recent estimate run(s) for a scenario."""
    _require_postgres()
    if day_of_week:
        day_of_week = _require_valid_day(day_of_week)
    with pg() as conn:
        with conn.cursor() as cur:
            filters = ["scenario_id = %s", "status = 'complete'"]
            params: List[Any] = [scenario_id]
            if technician_id:
                filters.append("technician_id = %s")
                params.append(technician_id)
            if day_of_week:
                filters.append("service_day = %s")
                params.append(day_of_week)
            cur.execute(
                f"""
                SELECT DISTINCT ON (technician_id, service_day)
                    id, technician_id, service_day,
                    total_distance_meters, total_duration_seconds,
                    customer_to_customer_distance_meters,
                    customer_to_customer_duration_seconds,
                    start_to_first_distance_meters,
                    start_to_first_duration_seconds,
                    last_to_end_distance_meters,
                    last_to_end_duration_seconds,
                    service_duration_seconds, total_work_duration_seconds,
                    stop_count, request_count, cache_hit_count,
                    warnings, is_stale, created_at
                FROM route_estimate_runs
                WHERE {" AND ".join(filters)}
                ORDER BY technician_id, service_day, created_at DESC
                """,
                params,
            )
            rows = [dict(r) for r in cur.fetchall()]

    def _mi(m): return round((m or 0) / 1609.34, 1) if m is not None else None
    def _min(s): return round((s or 0) / 60) if s is not None else None

    items = []
    for r in rows:
        r["source_account_id"] = r.get("technician_id")
        r["day_of_week"] = r.get("service_day")
        r["total_distance_miles"] = _mi(r.pop("total_distance_meters", None))
        r["total_duration_minutes"] = _min(r.pop("total_duration_seconds", None))
        r["customer_to_customer_distance_miles"] = _mi(r.pop("customer_to_customer_distance_meters", None))
        r["customer_to_customer_duration_minutes"] = _min(r.pop("customer_to_customer_duration_seconds", None))
        r["start_to_first_distance_miles"] = _mi(r.pop("start_to_first_distance_meters", None))
        r["start_to_first_duration_minutes"] = _min(r.pop("start_to_first_duration_seconds", None))
        r["last_to_end_distance_miles"] = _mi(r.pop("last_to_end_distance_meters", None))
        r["last_to_end_duration_minutes"] = _min(r.pop("last_to_end_duration_seconds", None))
        r["service_duration_minutes"] = _min(r.pop("service_duration_seconds", None))
        r["total_work_duration_minutes"] = _min(r.pop("total_work_duration_seconds", None))
        items.append(r)

    return {"ok": True, "items": items, "estimates": items}


def optimize_route_order(
    scenario_id: int,
    technician_id: str,
    day_of_week: str,
    tech_profile: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Return an optimization preview for a single tech/day route.
    Does NOT mutate any scenario data — returns proposed order only.
    """
    from services import google_maps

    _require_postgres()
    if not google_maps.server_configured():
        raise HTTPException(status_code=400, detail="GOOGLE_MAPS_SERVER_API_KEY is not configured")
    if not google_maps.optimization_enabled():
        raise HTTPException(status_code=403, detail="Route optimization is disabled in Sentinel")
    day_of_week = _require_valid_day(day_of_week)

    stops = _stops_for_route(scenario_id, "scenario", technician_id, day_of_week)
    if len(stops) < 3:
        raise HTTPException(status_code=400, detail="Route needs at least 3 stops to optimize")

    missing_coords = [s for s in stops if s.get("latitude") is None or s.get("longitude") is None]
    if missing_coords:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot optimize route while {len(missing_coords)} stop(s) are missing coordinates",
        )

    start_lat, start_lng, start_type = _route_endpoint(tech_profile, "start")
    end_lat, end_lng, end_type = _route_endpoint(tech_profile, "end")
    start_lat = float(start_lat) if start_lat is not None else None
    start_lng = float(start_lng) if start_lng is not None else None
    end_lat = float(end_lat) if end_lat is not None else None
    end_lng = float(end_lng) if end_lng is not None else None

    valid_stops = [s for s in stops if s.get("latitude") is not None and s.get("longitude") is not None]

    if start_lat is None and valid_stops:
        start_lat = float(valid_stops[0]["latitude"])
        start_lng = float(valid_stops[0]["longitude"])
        intermediates_stops = valid_stops[1:]
    else:
        intermediates_stops = valid_stops

    if end_lat is None and valid_stops:
        end_lat = float(valid_stops[-1]["latitude"])
        end_lng = float(valid_stops[-1]["longitude"])
        if start_type in {"first_stop", "last_stop"}:
            intermediates_stops = valid_stops[1:-1]

    if len(intermediates_stops) < 1:
        raise HTTPException(status_code=400, detail="Not enough intermediate stops to optimize")

    intermediates = [
        {"location": {"latLng": {"latitude": float(s["latitude"]), "longitude": float(s["longitude"])}}}
        for s in intermediates_stops
    ]

    with pg() as conn:
        with conn.cursor() as cur:
            _check_and_record_google_maps_usage(cur, request_count=2, matrix_element_count=0)
        conn.commit()

    current_intermediates = [
        {"location": {"latLng": {"latitude": float(s["latitude"]), "longitude": float(s["longitude"])}}}
        for s in intermediates_stops
    ]
    current_result = google_maps.compute_route(
        start_lat, start_lng, end_lat, end_lng,
        intermediates=current_intermediates,
        optimize_waypoint_order=False,
    )
    if current_result.get("error"):
        raise HTTPException(status_code=502, detail=f"Google Maps error: {current_result['error']}")

    result = google_maps.compute_route(
        start_lat, start_lng, end_lat, end_lng,
        intermediates=intermediates,
        optimize_waypoint_order=True,
    )

    if result.get("error"):
        raise HTTPException(status_code=502, detail=f"Google Maps error: {result['error']}")

    optimized_indices = result.get("optimized_waypoint_order") or list(range(len(intermediates_stops)))

    # Build optimized stop ordering
    optimized_stops = [intermediates_stops[i] for i in optimized_indices]

    # Reconstruct full list with anchors
    if start_type not in {"first_stop", "last_stop"}:
        full_optimized = optimized_stops
    else:
        full_optimized = [valid_stops[0]] + optimized_stops

    if end_type not in {"first_stop", "last_stop"}:
        pass  # end anchor is home, not a stop
    elif valid_stops and valid_stops[-1] not in full_optimized:
        full_optimized = full_optimized + [valid_stops[-1]]

    current_dist_m = current_result.get("distance_meters", 0)
    current_dur_s = current_result.get("duration_seconds", 0)
    optimized_dist_m = result.get("distance_meters", 0)
    optimized_dur_s = result.get("duration_seconds", 0)
    reordered_count = sum(
        1
        for idx, stop in enumerate(full_optimized)
        if idx >= len(valid_stops) or stop.get("id") != valid_stops[idx].get("id")
    )

    return {
        "ok": True,
        "scenario_id": scenario_id,
        "technician_id": technician_id,
        "day_of_week": day_of_week,
        "stop_count": len(stops),
        "valid_stop_count": len(valid_stops),
        "skipped_count": len(stops) - len(valid_stops),
        "current_stop_order": [
            {"assignment_id": s.get("id"), "stop_order": s.get("stop_order"), "customer_name": s.get("customer_name"), "address": s.get("address")}
            for s in stops
        ],
        "optimized_stop_order": [
            {"assignment_id": s.get("id"), "stop_order": idx + 1, "customer_name": s.get("customer_name"), "address": s.get("address")}
            for idx, s in enumerate(full_optimized)
        ],
        "current_distance_miles": round(current_dist_m / 1609.34, 1),
        "current_duration_minutes": round(current_dur_s / 60),
        "optimized_distance_miles": round(optimized_dist_m / 1609.34, 1),
        "optimized_duration_minutes": round(optimized_dur_s / 60),
        "estimated_miles_saved": round((current_dist_m - optimized_dist_m) / 1609.34, 1),
        "estimated_minutes_saved": round((current_dur_s - optimized_dur_s) / 60),
        "stops_reordered": reordered_count,
        "polyline": result.get("polyline", ""),
        "warnings": [],
    }


def apply_optimized_order(
    scenario_id: int,
    technician_id: str,
    day_of_week: str,
    ordered_assignment_ids: List[int],
) -> Dict[str, Any]:
    """
    Apply a new stop order to scenario assignments.
    Only mutates route_scenario_assignments — never touches Skimmer/current route tables.
    """
    _require_postgres()

    detail = get_scenario(scenario_id)
    if detail["scenario"]["status"] not in SCENARIO_EDITABLE_STATUSES:
        raise HTTPException(status_code=409, detail="Scenario must be in draft status to apply optimization")

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM route_scenario_assignments
                WHERE scenario_id = %s
                  AND source_account_id = %s
                  AND day_of_week = %s
                ORDER BY stop_order ASC NULLS LAST, id ASC
                """,
                (scenario_id, technician_id, day_of_week),
            )
            existing_ids = [int(r["id"]) for r in cur.fetchall()]
            provided_ids = [int(i) for i in ordered_assignment_ids]
            if set(existing_ids) != set(provided_ids) or len(existing_ids) != len(provided_ids):
                raise HTTPException(
                    status_code=400,
                    detail="Optimized order must include exactly the assignments in this technician/day route",
                )
            for new_order, assignment_id in enumerate(ordered_assignment_ids, start=1):
                cur.execute(
                    """
                    UPDATE route_scenario_assignments
                    SET stop_order = %s,
                        is_changed_from_current = TRUE,
                        updated_at = NOW()
                    WHERE id = %s AND scenario_id = %s
                      AND source_account_id = %s AND day_of_week = %s
                    """,
                    (new_order, assignment_id, scenario_id, technician_id, day_of_week),
                )
            _mark_change_plans_stale(cur, scenario_id)
            _mark_route_estimates_stale(cur, scenario_id, technician_id, day_of_week)
        conn.commit()

    return {"ok": True, "updated_count": len(ordered_assignment_ids)}
