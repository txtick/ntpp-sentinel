import json
import logging
import math
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


def _require_postgres() -> None:
    if not DATABASE_URL:
        raise HTTPException(status_code=404, detail="DATABASE_URL is not configured")


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
        if ordered and start_type in ("home", "custom"):
            slat = tech_profile.get("home_latitude") if start_type == "home" else tech_profile.get("custom_start_latitude")
            slon = tech_profile.get("home_longitude") if start_type == "home" else tech_profile.get("custom_start_longitude")
            first = next((s for s in ordered if s.get("latitude") is not None), None)
            if first and slat is not None:
                d = _haversine_miles(slat, slon, first.get("latitude"), first.get("longitude"))
                if d is not None and d > WARN_COMMUTE_MILES:
                    warnings.append(f"First stop is {d:.1f} mi from tech start location — long commute.")
        if ordered and end_type in ("home", "custom"):
            elat = tech_profile.get("home_latitude") if end_type == "home" else tech_profile.get("custom_end_latitude")
            elon = tech_profile.get("home_longitude") if end_type == "home" else tech_profile.get("custom_end_longitude")
            last = next((s for s in reversed(ordered) if s.get("latitude") is not None), None)
            if last and elat is not None:
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

    start_miles: Optional[float] = None
    end_miles: Optional[float] = None
    start_type = (tech_profile or {}).get("default_start_location_type", "first_stop")
    end_type = (tech_profile or {}).get("default_end_location_type", "last_stop")
    include_start = (tech_profile or {}).get("include_start_drive_in_metrics", True)
    include_end = (tech_profile or {}).get("include_end_drive_in_metrics", True)

    if tech_profile and include_start and start_type in ("home", "custom"):
        slat = tech_profile.get("home_latitude") if start_type == "home" else tech_profile.get("custom_start_latitude")
        slon = tech_profile.get("home_longitude") if start_type == "home" else tech_profile.get("custom_start_longitude")
        first = next((s for s in ordered if s.get("latitude") is not None), None)
        if first and slat is not None:
            start_miles = _haversine_miles(slat, slon, first.get("latitude"), first.get("longitude"))

    if tech_profile and include_end and end_type in ("home", "custom"):
        elat = tech_profile.get("home_latitude") if end_type == "home" else tech_profile.get("custom_end_latitude")
        elon = tech_profile.get("home_longitude") if end_type == "home" else tech_profile.get("custom_end_longitude")
        last = next((s for s in reversed(ordered) if s.get("latitude") is not None), None)
        if last and elat is not None:
            end_miles = _haversine_miles(elat, elon, last.get("latitude"), last.get("longitude"))

    commute_miles = (start_miles or 0.0) + (end_miles or 0.0)
    total_miles = round(in_route_miles + commute_miles, 2)
    # Rough estimate: 25 mph avg
    drive_time_minutes = round((total_miles / 25.0) * 60)
    total_time_minutes = service_minutes + drive_time_minutes

    return {
        "pool_count": len(stops),
        "service_minutes": service_minutes,
        "in_route_miles": round(in_route_miles, 2),
        "start_miles": round(start_miles, 2) if start_miles is not None else None,
        "end_miles": round(end_miles, 2) if end_miles is not None else None,
        "commute_miles": round(commute_miles, 2),
        "total_miles": total_miles,
        "drive_time_minutes": drive_time_minutes,
        "total_time_minutes": total_time_minutes,
        "missing_coords_count": sum(1 for s in stops if s.get("latitude") is None or s.get("longitude") is None),
    }


# ── Current routes (from Skimmer data) ────────────────────────────────────────

def list_current_route_pools(source_system: str = "skimmer") -> Dict[str, Any]:
    _require_postgres()
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

def list_technician_profiles() -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.*,
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
                """
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
    values = list(data.values()) + [technician_id]
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
    valid_statuses = {"draft", "locked", "approved", "pushed", "archived"}
    data = {k: v for k, v in payload.items() if k in allowed and v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields provided")
    if "status" in data and data["status"] not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(valid_statuses)}")
    set_clauses = [f"{k} = %s" for k in data] + ["updated_at = NOW()"]
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE route_scenarios SET {', '.join(set_clauses)} WHERE id = %s RETURNING *",
                list(data.values()) + [scenario_id],
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Scenario not found")
        conn.commit()
    return {"ok": True, "scenario": dict(row)}


def create_scenario_from_current(name: str, notes: str = "", created_by: str = "", source_system: str = "skimmer") -> Dict[str, Any]:
    """Snapshot the current Skimmer route data into a new sandbox scenario."""
    _require_postgres()
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
    valid_days = set(DAY_ORDER.keys())
    if new_day_of_week not in valid_days:
        raise HTTPException(status_code=400, detail=f"day_of_week must be one of: {', '.join(sorted(valid_days))}")
    with pg() as conn:
        with conn.cursor() as cur:
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
        conn.commit()
    return {"ok": True, "assignment": dict(updated) if updated else {}}


def reorder_assignments(scenario_id: int, ordered_ids: List[int]) -> Dict[str, Any]:
    """Reassign stop_order based on the provided ordered list of assignment IDs."""
    _require_postgres()
    if not ordered_ids:
        raise HTTPException(status_code=400, detail="ordered_ids must not be empty")
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM route_scenario_assignments WHERE scenario_id = %s AND id = ANY(%s)",
                (scenario_id, ordered_ids),
            )
            found_ids = {r["id"] for r in cur.fetchall()}
            missing = [i for i in ordered_ids if i not in found_ids]
            if missing:
                raise HTTPException(status_code=404, detail=f"Assignment IDs not found in scenario: {missing}")
            for order, aid in enumerate(ordered_ids, start=1):
                cur.execute(
                    "UPDATE route_scenario_assignments SET stop_order = %s, updated_at = NOW() WHERE id = %s AND scenario_id = %s",
                    (order * 10, aid, scenario_id),
                )
            cur.execute("UPDATE route_scenarios SET updated_at = NOW() WHERE id = %s", (scenario_id,))
        conn.commit()
    return {"ok": True, "reordered": len(ordered_ids)}


# ── Validation ────────────────────────────────────────────────────────────────

def validate_scenario(scenario_id: int) -> Dict[str, Any]:
    _require_postgres()
    detail = get_scenario(scenario_id)
    scenario = detail["scenario"]
    route_groups = detail["route_groups"]

    all_warnings: List[Dict[str, Any]] = []
    seen_locations: Dict[str, List[str]] = {}

    for group in route_groups:
        key = f"{group['tech_name']} / {group['day_of_week']}"
        for w in group.get("warnings", []):
            all_warnings.append({"group": key, "message": w})
        for stop in group["stops"]:
            loc = stop.get("source_service_location_id", "")
            seen_locations.setdefault(loc, []).append(key)

    # Duplicate location across route groups
    for loc, groups in seen_locations.items():
        if len(groups) > 1:
            all_warnings.append({
                "group": "global",
                "message": f"Service location appears in multiple route groups: {', '.join(groups)}",
            })

    return {
        "ok": True,
        "scenario_id": scenario_id,
        "scenario_name": scenario.get("name"),
        "warning_count": len(all_warnings),
        "warnings": all_warnings,
        "valid": len(all_warnings) == 0,
    }


# ── Comparison (scenario vs current) ─────────────────────────────────────────

def get_comparison(scenario_id: int, source_system: str = "skimmer") -> Dict[str, Any]:
    _require_postgres()
    current = list_current_route_pools(source_system)
    scenario_detail = get_scenario(scenario_id)

    # Build current lookup by location_id → (account_id, day, order)
    current_map: Dict[str, Dict[str, Any]] = {}
    for group in current["route_groups"]:
        for stop in group["stops"]:
            loc = stop["source_service_location_id"]
            current_map[loc] = {
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
    }


# ── Change plan ────────────────────────────────────────────────────────────────

def generate_change_plan(scenario_id: int) -> Dict[str, Any]:
    _require_postgres()
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

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO route_change_plans (scenario_id, summary_json)
                VALUES (%s, %s::jsonb)
                RETURNING *
                """,
                (scenario_id, json.dumps(summary)),
            )
            plan_row = cur.fetchone()
            plan_id = plan_row["id"]
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
        conn.commit()

    return {"ok": True, "change_plan_id": plan_id, "change_plan": dict(plan_row), "summary": summary, "items": items}


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
                UPDATE route_change_plans
                SET status = 'approved', approved_by = %s, approved_at = NOW(), updated_at = NOW()
                WHERE id = %s AND status = 'generated'
                RETURNING *
                """,
                (approved_by.strip() or "user", plan_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Change plan not found or not in generated status")
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
                    "UPDATE route_change_plans SET status = 'manually_completed' WHERE id = %s AND status IN ('approved', 'manually_in_progress')",
                    (plan_id,),
                )
            elif counts["completed"] > 0:
                cur.execute(
                    "UPDATE route_change_plans SET status = 'manually_in_progress' WHERE id = %s AND status = 'approved'",
                    (plan_id,),
                )
        conn.commit()
    return {"ok": True, "item": dict(row)}


def mark_plan_printed(plan_id: int) -> Dict[str, Any]:
    _require_postgres()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE route_change_plans SET status = 'printed' WHERE id = %s AND status IN ('generated', 'approved') RETURNING *",
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
