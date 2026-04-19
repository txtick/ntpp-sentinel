import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from pg import DATABASE_URL, ensure_pg_schema, pg, pg_healthcheck
from services.dashboard_schema import ensure_dashboard_schema_definitions

MANAGED_ALERT_CATEGORIES = ("pool", "process", "revenue")
ACTIONABLE_ALERT_STATUSES = ("open", "acknowledged", "snoozed", "resolved", "cleared")
MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD = float(
    os.getenv("MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD", "75")
)
DASHBOARD_TIMEZONE = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))
SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com").rstrip("/")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "").strip()
GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
GHL_TOKEN = os.getenv("GHL_TOKEN", "").strip()
GHL_VERSION = os.getenv("GHL_VERSION", "2021-07-28").strip()
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "").strip()
LABOR_POOL_RATE = float(os.getenv("LABOR_POOL_RATE", "16"))
LABOR_FILTER_CLEAN_RATE = float(os.getenv("LABOR_FILTER_CLEAN_RATE", "25"))
LABOR_REGULAR_POOL_CAP = max(0, int(os.getenv("LABOR_REGULAR_POOL_CAP", "40")))
LABOR_SALARY_TECH_NAMES = tuple(
    part.strip().lower()
    for part in os.getenv("LABOR_SALARY_TECH_NAMES", "jarrett,jim").split(",")
    if part.strip()
)
DEFAULT_CUSTOMER_CHART_POLICY: Dict[str, Any] = {
    "default_days": 90,
    "range_days": [30, 90, 180, 365],
    "hidden_metrics": ["total_chlorine", "combined_chlorine"],
    "sparse_metrics": [],
    "required_every_visit_metrics": [
        "free_chlorine",
        "ph",
        "temperature",
        "tds",
        "alkalinity",
        "lsi",
        "salt",
        "filter_pressure",
    ],
    "monthly_metrics": [
        "phosphates",
        "calcium_hardness",
        "cya",
    ],
    "chart_order": [
        "free_chlorine",
        "ph",
        "filter_pressure",
        "temperature",
        "alkalinity",
        "lsi",
        "cya",
        "calcium_hardness",
        "phosphates",
        "salt",
        "tds",
    ],
    "recommended_highs": {
        "free_chlorine": 5,
        "ph": 9.36,
        "temperature": 98.4,
        "tds": 2400,
        "alkalinity": 144,
        "lsi": 0.36,
        "salt": 4080,
        "filter_pressure": 20,
        "phosphates": 600,
        "calcium_hardness": 480,
        "cya": 60,
    },
    "display_precision": {
        "ph": 2,
        "lsi": 2,
    },
    "metric_labels": {
        "ph": "pH",
        "free_chlorine": "Free Chlorine",
        "total_chlorine": "Total Chlorine",
        "combined_chlorine": "Combined Chlorine",
        "cya": "CYA",
        "alkalinity": "Alkalinity",
        "calcium_hardness": "Calcium Hardness",
        "filter_pressure": "Filter Pressure",
        "salt": "Salt",
        "phosphates": "Phosphates",
        "temperature": "Temperature",
        "tds": "TDS",
        "lsi": "LSI",
    },
}
CUSTOMER_CHART_POLICY_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "config", "customer_chart_policy.json")
)
FILTER_CLEAN_QUOTE_REMINDER_SOURCE = "filter_clean_quote_notify"
FILTER_CLEAN_RULE_CODES = {
    "filter_clean_trend",
    "filter_clean_missing_psi",
    "freedom_filter_clean_not_scheduled",
}
DEFAULT_FILTER_CLEAN_NOTIFY_SMS = (
    "North Texas Pool Pros here. Based on your filter PSI readings and your recent filter-clean history, "
    "you are due for a filter clean. We will send a quote for your approval shortly. "
    "If you would like to proceed, simply approve the quote. If not, you can reject it."
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


def _now_tz() -> datetime:
    return datetime.now(ZoneInfo(DASHBOARD_TIMEZONE))


def _skimmer_json_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    if not SKIMMER_API_KEY:
        raise HTTPException(status_code=503, detail="SKIMMER_API_KEY is not configured")
    url = f"{SKIMMER_API_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"skimmer-api-key": SKIMMER_API_KEY, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Skimmer API error {exc.code}: {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Skimmer API request failed: {exc}")
    try:
        return json.loads(body)
    except Exception:
        raise HTTPException(status_code=502, detail="Skimmer API returned invalid JSON")


def _ghl_headers() -> Dict[str, str]:
    if not GHL_TOKEN:
        raise HTTPException(status_code=503, detail="GHL_TOKEN is not configured")
    if not GHL_LOCATION_ID:
        raise HTTPException(status_code=503, detail="GHL_LOCATION_ID is not configured")
    return {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": GHL_VERSION,
        "LocationId": GHL_LOCATION_ID,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _ghl_json_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{GHL_BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=_ghl_headers(), method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"GHL {method.upper()} {path} failed: {exc.code} {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"GHL {method.upper()} {path} request failed: {exc}")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        raise HTTPException(status_code=502, detail=f"GHL {method.upper()} {path} returned invalid JSON")


def _extract_list_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "data", "results", "quotes", "value"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("items") or value.get("data") or value.get("results")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
    return []


def _recursive_values(value: Any) -> List[Any]:
    if isinstance(value, dict):
        collected: List[Any] = []
        for key, item in value.items():
            collected.append(key)
            collected.extend(_recursive_values(item))
        return collected
    if isinstance(value, list):
        collected = []
        for item in value:
            collected.extend(_recursive_values(item))
        return collected
    return [value]


def _recursive_key_values(payload: Any, wanted_keys: set[str]) -> List[str]:
    matches: List[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).strip().lower() in wanted_keys and value is not None:
                matches.append(str(value).strip())
            matches.extend(_recursive_key_values(value, wanted_keys))
    elif isinstance(payload, list):
        for item in payload:
            matches.extend(_recursive_key_values(item, wanted_keys))
    return matches


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_filter_clean_text(value: Any) -> bool:
    text = _normalize_token(value)
    return "filter clean" in text or "filter-clean" in text


def _default_filter_clean_quote_due_at() -> datetime:
    now_local = _now_tz()
    due_local = (now_local + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return due_local


def _filter_clean_reminder_title(alert: Dict[str, Any]) -> str:
    metadata = alert.get("metadata_json") or {}
    customer_name = str(metadata.get("customer_name") or "").strip()
    pool_name = str(metadata.get("pool_name") or "").strip()
    who = customer_name or "Customer"
    if pool_name:
        who = f"{who} ({pool_name})"
    return f"Send filter clean quote: {who}"[:250]


def _filter_clean_reminder_summary(alert: Dict[str, Any], note: Optional[str]) -> str:
    metadata = alert.get("metadata_json") or {}
    reason = str(alert.get("summary") or "").strip()
    suffix = str(note or "").strip()
    customer_name = str(metadata.get("customer_name") or "").strip()
    if suffix:
        return suffix[:2000]
    if reason and customer_name:
        return f"Notify {customer_name} and send the promised filter clean quote. {reason}"[:2000]
    return (reason or "Notify customer and send the promised filter clean quote.")[:2000]


def _skimmer_quote_endpoints() -> List[str]:
    return [
        "/Quotes",
        "/Quotes/GetAllQuotes",
    ]


def _fetch_skimmer_quotes() -> tuple[List[Dict[str, Any]], Optional[str]]:
    last_error: Optional[str] = None
    for path in _skimmer_quote_endpoints():
        try:
            payload = _skimmer_json_get(path)
        except HTTPException as exc:
            last_error = str(exc.detail)
            continue
        items = _extract_list_items(payload)
        if items:
            return items, path
        if isinstance(payload, dict):
            return [], path
    return [], last_error


def _quote_matches_filter_clean(
    quote: Dict[str, Any],
    *,
    source_customer_id: Optional[str],
    source_service_location_id: Optional[str],
) -> bool:
    customer_ids = {
        _normalize_token(value)
        for value in _recursive_key_values(
            quote,
            {
                "customerid",
                "sourcecustomerid",
                "customer_id",
                "source_customer_id",
                "clientid",
            },
        )
        if _normalize_token(value)
    }
    service_location_ids = {
        _normalize_token(value)
        for value in _recursive_key_values(
            quote,
            {
                "servicelocationid",
                "sourceservicelocationid",
                "service_location_id",
                "source_service_location_id",
                "poolid",
                "sourcepoolid",
            },
        )
        if _normalize_token(value)
    }

    customer_match = bool(source_customer_id) and _normalize_token(source_customer_id) in customer_ids
    location_match = bool(source_service_location_id) and _normalize_token(source_service_location_id) in service_location_ids
    if not customer_match and not location_match:
        return False

    top_level_text = []
    for key in (
        "name",
        "title",
        "description",
        "quoteName",
        "quoteTitle",
        "subject",
        "templateName",
        "type",
    ):
        value = quote.get(key)
        if value is not None:
            top_level_text.append(str(value))

    nested_text = [str(value) for value in _recursive_values(quote) if isinstance(value, str)]
    return any(_is_filter_clean_text(value) for value in top_level_text + nested_text)


def _find_matching_filter_clean_quote(
    quotes: List[Dict[str, Any]],
    *,
    source_customer_id: Optional[str],
    source_service_location_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    for quote in quotes:
        if _quote_matches_filter_clean(
            quote,
            source_customer_id=source_customer_id,
            source_service_location_id=source_service_location_id,
        ):
            return quote
    return None


def _extract_quote_identifier(quote: Dict[str, Any]) -> Optional[str]:
    for key in ("id", "quoteId", "sourceQuoteId", "quoteNumber", "number"):
        value = quote.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _extract_conversation_id(payload: Any) -> Optional[str]:
    if isinstance(payload, dict):
        for key in ("conversations", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    for id_key in ("id", "conversationId"):
                        raw = first.get(id_key)
                        if raw is not None and str(raw).strip():
                            return str(raw).strip()
        for id_key in ("id", "conversationId"):
            raw = payload.get(id_key)
            if raw is not None and str(raw).strip():
                return str(raw).strip()
    return None


def _ghl_find_conversation_id(contact_id: Optional[str], phone: Optional[str]) -> Optional[str]:
    params: Dict[str, Any] = {}
    if contact_id:
        params["contactId"] = contact_id
    elif phone:
        params["phone"] = phone
    else:
        return None

    payload = _ghl_json_request("GET", "/conversations/search", params=params)
    conversation_id = _extract_conversation_id(payload)
    if conversation_id:
        return conversation_id
    if contact_id and phone:
        payload = _ghl_json_request("GET", "/conversations/search", params={"phone": phone})
        return _extract_conversation_id(payload)
    return None


def _ghl_send_sms(conversation_id: str, contact_id: str, message_text: str) -> Any:
    return _ghl_json_request(
        "POST",
        "/conversations/messages",
        payload={
            "type": "SMS",
            "message": message_text,
            "conversationId": conversation_id,
            "contactId": contact_id,
        },
    )


def _merge_customer_chart_policy(override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    policy = json.loads(json.dumps(DEFAULT_CUSTOMER_CHART_POLICY))
    if not isinstance(override, dict):
        return policy

    for key in ("default_days",):
        value = override.get(key)
        if isinstance(value, int) and value > 0:
            policy[key] = value

    for key in (
        "range_days",
        "hidden_metrics",
        "sparse_metrics",
        "required_every_visit_metrics",
        "monthly_metrics",
        "chart_order",
    ):
        value = override.get(key)
        if isinstance(value, list):
            policy[key] = list(value)

    labels = override.get("metric_labels")
    if isinstance(labels, dict):
        policy["metric_labels"].update(labels)

    highs = override.get("recommended_highs")
    if isinstance(highs, dict):
        policy["recommended_highs"].update(highs)

    precision = override.get("display_precision")
    if isinstance(precision, dict):
        policy["display_precision"].update(precision)

    return policy


def _load_customer_chart_policy() -> Dict[str, Any]:
    try:
        with open(CUSTOMER_CHART_POLICY_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return _merge_customer_chart_policy(None)
    except Exception:
        return _merge_customer_chart_policy(None)
    return _merge_customer_chart_policy(raw)


def _metric_label(reading_key: Any) -> str:
    value = str(reading_key or "").strip().lower()
    if not value:
        return "Alert"
    if value == "fc_cya_ratio":
        return "FC:CYA Ratio"
    label = (DEFAULT_CUSTOMER_CHART_POLICY.get("metric_labels") or {}).get(value)
    if label:
        return str(label)
    return value.replace("_", " ").title()


def _rule_label(rule_code: str) -> str:
    labels = {
        "filter_clean_missing_psi": "No PSI in 90 Days",
        "filter_clean_trend": "Filter Pressure Stays High",
        "freedom_filter_clean_not_scheduled": "Freedom Filter Clean Not Scheduled",
        "chemical_cost_review_high": "Chemical Cost Review",
        "drain_refill_cya_repeat": "Drain / Refill Review",
        "fc_cya_ratio_bad_2wk": "FC:CYA Ratio Out Of Balance",
        "ph_bad_2_of_2_14d": "pH High Two Weeks In A Row",
        "cya_above_100": "CYA Above 100",
    }
    if rule_code in labels:
        return labels[rule_code]
    return rule_code.replace("_", " ").title()


def _view_exists(cur, view_name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) AS exists_name", (f"public.{view_name}",))
    row = cur.fetchone()
    return bool(row and row.get("exists_name"))


def _normalize_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    value = str(status).strip().lower()
    return value or None


def _fetch_alert_instance(cur, alert_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            id,
            category,
            rule_code,
            entity_type,
            entity_id,
            customer_id,
            pool_id,
            status,
            severity,
            title,
            summary,
            first_detected_at,
            last_detected_at,
            last_evaluated_at,
            resolved_at,
            cleared_at,
            assigned_to,
            acknowledged_at,
            snoozed_until,
            metadata_json,
            created_at,
            updated_at
        FROM alert_instances
        WHERE id = %s
        """,
        (alert_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _fetch_reminder_instance(cur, reminder_id: int) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT
            r.id,
            r.source_type,
            r.source_alert_instance_id,
            r.customer_id,
            r.pool_id,
            r.technician_id,
            r.status,
            r.priority,
            r.title,
            r.summary,
            r.due_at,
            r.snoozed_until,
            r.completed_at,
            r.canceled_at,
            r.assigned_to,
            r.created_by,
            r.metadata_json,
            r.created_at,
            r.updated_at,
            a.category AS source_alert_category,
            a.rule_code AS source_alert_rule_code,
            a.severity AS source_alert_severity,
            a.title AS source_alert_title,
            COALESCE(
                NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                NULLIF(c.company_name, ''),
                NULL
            ) AS customer_name,
            p.name AS pool_name,
            COALESCE(
                NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''),
                NULLIF(t.username, ''),
                NULL
            ) AS technician_name
        FROM reminder_instances r
        LEFT JOIN alert_instances a ON a.id = r.source_alert_instance_id
        LEFT JOIN customers c ON c.id = r.customer_id
        LEFT JOIN pools p ON p.id = r.pool_id
        LEFT JOIN technicians t ON t.id = r.technician_id
        WHERE r.id = %s
        """,
        (reminder_id,),
    )
    row = cur.fetchone()
    return dict(row) if row else None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace(" ", "T")
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except Exception:
        return None


def _default_labor_week(anchor: Optional[date] = None) -> tuple[date, date]:
    if anchor is None:
        anchor = datetime.now(ZoneInfo(DASHBOARD_TIMEZONE)).date()
    days_since_sunday = (anchor.weekday() + 1) % 7
    week_start = anchor - timedelta(days=days_since_sunday)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def _is_salary_tech(item: Dict[str, Any]) -> bool:
    if not LABOR_SALARY_TECH_NAMES:
        return False
    candidates = {
        str(item.get("first_name") or "").strip().lower(),
        str(item.get("tech_name") or "").strip().lower(),
        str(item.get("username") or "").strip().lower(),
    }
    split_name = str(item.get("tech_name") or "").strip().lower().split()
    if split_name:
        candidates.add(split_name[0])
    return any(candidate in LABOR_SALARY_TECH_NAMES for candidate in candidates if candidate)


def _fetch_skimmer_routes_for_day(service_date: date) -> List[Dict[str, Any]]:
    if not SKIMMER_API_KEY:
        raise HTTPException(status_code=503, detail="SKIMMER_API_KEY is not configured")

    query = urllib.parse.urlencode({"ServiceDate": service_date.isoformat()})
    url = f"{SKIMMER_API_BASE_URL}/Routes/GetAllRoutesForDay?{query}"
    req = urllib.request.Request(
        url,
        headers={"skimmer-api-key": SKIMMER_API_KEY, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Skimmer API error {exc.code}: {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Skimmer API request failed: {exc}")

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=502, detail="Skimmer API returned invalid JSON")
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Unexpected response shape from Skimmer routes API")
    return data


def _coerce_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes"}:
            return True
        if token in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _route_stop_complete(stop: Dict[str, Any]) -> bool:
    complete_value = None
    for key in ("isCompleted", "completed", "isComplete"):
        if key in stop:
            complete_value = _coerce_bool(stop.get(key))
            if complete_value is not None:
                return complete_value
    return bool(stop.get("completeTime") or stop.get("completedAt") or stop.get("completeDate") or stop.get("endTime"))


def _route_stop_skipped(stop: Dict[str, Any]) -> bool:
    for key in ("isSkipped", "skipped", "isStopSkipped"):
        if key in stop:
            skipped = _coerce_bool(stop.get(key))
            if skipped is not None:
                return skipped
    return False


def _skimmer_api_cleaning_counts(start_day: date, end_day: date) -> Dict[str, Dict[str, Any]]:
    counts: Dict[str, Dict[str, Any]] = {}
    current = start_day
    while current <= end_day:
        routes = _fetch_skimmer_routes_for_day(current)
        for route in routes:
            tech_id = str(route.get("techId") or "").strip()
            if not tech_id:
                continue
            tech_first = str(route.get("techFirstName") or "").strip()
            tech_last = str(route.get("techLastName") or "").strip()
            bucket = counts.setdefault(
                tech_id,
                {
                    "tech_id": tech_id,
                    "tech_name": f"{tech_first} {tech_last}".strip(),
                    "pool_count": 0,
                    "completed_service_days": set(),
                },
            )
            for stop in route.get("stops") or []:
                if _route_stop_skipped(stop) or not _route_stop_complete(stop):
                    continue
                bucket["pool_count"] += 1
                bucket["completed_service_days"].add(current.isoformat())
        current += timedelta(days=1)
    return counts


def _detection_evidence_ts(item: Dict[str, Any]) -> Optional[datetime]:
    return _parse_dt(item.get("service_date"))


def _metadata_source_refresh_id(item: Dict[str, Any]) -> Optional[int]:
    try:
        raw = (item.get("metadata_json") or {}).get("refresh_run_id")
        return int(raw) if raw is not None else None
    except Exception:
        return None


def _ensure_backend_owned_dashboard_definitions() -> None:
    ensure_pg_schema()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('public.customers') AS customers_name,
                       to_regclass('public.pools') AS pools_name,
                       to_regclass('public.chemistry_readings') AS chemistry_name,
                       to_regclass('public.chemical_dose_events') AS dose_name,
                       to_regclass('public.ingest_pipeline_runs') AS runs_name
                """
            )
            row = cur.fetchone() or {}

        required = [
            row.get("customers_name"),
            row.get("pools_name"),
            row.get("chemistry_name"),
            row.get("dose_name"),
            row.get("runs_name"),
        ]
        if not all(required):
            return

        ensure_dashboard_schema_definitions(
            conn,
            monthly_chemical_cost_review_threshold=MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD,
        )
        conn.commit()


def _alert_title(category: str, row: Dict[str, Any]) -> str:
    customer_name = row.get("customer_name") or "Unknown Customer"
    pool_name = row.get("pool_name") or "No Pool"
    reading_key = _metric_label(row.get("reading_key") or row.get("opportunity_type") or "alert")
    if category == "revenue":
        return f"{customer_name}: {_rule_label(str(row.get('rule_code') or row.get('opportunity_type') or 'revenue_opportunity'))}"
    return f"{customer_name}: {reading_key} on {pool_name}"


def _alert_summary(category: str, row: Dict[str, Any]) -> str:
    rule_code = str(row.get("rule_code") or "")
    if category == "revenue":
        if rule_code == "filter_clean_missing_psi":
            window_days = row.get("window_days") or row.get("metadata_json", {}).get("window_days") or 90
            return f"No filter pressure reading in the last {window_days} days."
        if rule_code == "filter_clean_trend":
            return "Filter pressure has been 20 PSI or higher on the last 2 readings."
        if rule_code == "freedom_filter_clean_not_scheduled":
            return "Freedom customer has no upcoming filter clean work order scheduled."
        if rule_code == "chemical_cost_review_high":
            observed = row.get("observed_count")
            threshold = row.get("threshold_value")
            if observed is not None and threshold is not None:
                return f"Chemical cost was {observed} against review threshold {threshold}."
            return "Chemical cost is above the monthly review threshold."
        if rule_code == "drain_refill_cya_repeat":
            return "CYA has stayed high enough long enough to consider a drain and refill."
        return _rule_label(rule_code)

    observed_value = row.get("value")
    if observed_value is None:
        observed_value = row.get("observed_value")
    threshold_value = row.get("threshold_value")
    reading_key = row.get("reading_key") or "metric"
    if rule_code == "ph_bad_2_of_2_14d":
        return f"pH was above {threshold_value} on the last 2 readings."
    if rule_code == "cya_above_100":
        return "CYA is above 100."
    if reading_key == "fc_cya_ratio" and observed_value is not None and threshold_value is not None:
        try:
            observed_pct = float(observed_value) * 100.0
            threshold_pct = float(threshold_value) * 100.0
            return f"FC:CYA ratio was {observed_pct:.1f}% vs minimum {threshold_pct:.1f}% on the last 2 paired readings"
        except Exception:
            pass
    if threshold_value is None:
        return f"{_metric_label(reading_key)} alert detected"
    return f"{_metric_label(reading_key)} was {observed_value} against threshold {threshold_value}."


def _build_metadata(category: str, row: Dict[str, Any], refresh_run_id: int) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "refresh_run_id": refresh_run_id,
        "service_date": str(row.get("service_date")) if row.get("service_date") is not None else None,
    }
    for key in (
        "customer_name",
        "pool_name",
        "reading_key",
        "reading_type",
        "description",
        "unit_of_measure",
        "threshold_value",
        "value",
        "observed_value",
        "observed_count",
        "opportunity_type",
    ):
        if row.get(key) is not None:
            metadata[key] = row.get(key)
    metadata["source_category"] = category
    return metadata


def _build_pool_alert_metadata(cur, category: str, row: Dict[str, Any], refresh_run_id: int) -> Dict[str, Any]:
    metadata = _build_metadata(category, row, refresh_run_id)
    tech_context = _fetch_revenue_technician_context(cur, row.get("pool_id"))
    if tech_context:
        metadata.update(tech_context)
    return metadata


def _fetch_revenue_rule_context(cur, rule_code: Optional[str]) -> Dict[str, Any]:
    if not rule_code:
        return {}
    cur.execute(
        """
        SELECT
            rule_code,
            opportunity_type,
            threshold_value,
            window_days,
            description
        FROM revenue_rule_config
        WHERE rule_code = %s
        """,
        (str(rule_code),),
    )
    row = cur.fetchone()
    return dict(row) if row else {}


def _fetch_revenue_technician_context(cur, pool_id: Optional[int]) -> Dict[str, Any]:
    if pool_id is None:
        return {}

    payload: Dict[str, Any] = {}

    cur.execute(
        """
        SELECT source_system, source_service_location_id
        FROM pools
        WHERE id = %s
        """,
        (int(pool_id),),
    )
    pool_row = cur.fetchone()
    if not pool_row or not pool_row.get("source_service_location_id"):
        return payload

    source_system = pool_row.get("source_system")
    source_service_location_id = pool_row.get("source_service_location_id")

    cur.execute(
        """
        SELECT
            t.id AS technician_id,
            t.source_account_id AS tech_id,
            COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), NULLIF(t.username, ''), t.source_account_id) AS tech_name,
            t.role_type,
            a.day_of_week,
            a.frequency,
            a.start_date,
            a.end_date
        FROM service_location_technician_assignments a
        JOIN technicians t ON t.id = a.technician_id
        WHERE a.source_system = %s
          AND a.source_service_location_id = %s
          AND a.is_deleted = FALSE
          AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
        ORDER BY
            a.sequence ASC NULLS LAST,
            a.start_date DESC NULLS LAST,
            a.id DESC
        LIMIT 1
        """,
        (source_system, source_service_location_id),
    )
    current_assignment = cur.fetchone()
    if current_assignment:
        payload["assigned_technician"] = dict(current_assignment)

    cur.execute(
        """
        SELECT
            t.id AS technician_id,
            t.source_account_id AS tech_id,
            COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), NULLIF(t.username, ''), t.source_account_id) AS tech_name,
            t.role_type,
            s.service_date
        FROM technician_route_stops s
        JOIN technicians t ON t.id = s.technician_id
        WHERE s.source_system = %s
          AND s.source_service_location_id = %s
          AND s.is_skipped = FALSE
        ORDER BY s.service_date DESC, s.sequence ASC NULLS LAST
        LIMIT 1
        """,
        (source_system, source_service_location_id),
    )
    recent_service = cur.fetchone()
    if recent_service:
        payload["recent_service_technician"] = dict(recent_service)

    return payload


def _fetch_revenue_visit_breakdown(cur, pool_id: Optional[int], window_days: Optional[int]) -> List[Dict[str, Any]]:
    if pool_id is None:
        return []

    safe_window_days = int(window_days or 30)
    cur.execute(
        """
        SELECT
            d.service_date,
            d.description,
            d.dosage_key,
            d.unit_of_measure,
            d.quantity,
            d.entry_cost,
            d.estimated_cost,
            COALESCE(
                NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''),
                NULLIF(t.username, ''),
                NULL
            ) AS technician_name,
            t.source_account_id AS technician_tech_id
        FROM chemical_dose_events d
        JOIN pools p ON p.id = d.pool_id
        LEFT JOIN technician_route_stops s
          ON s.source_system = p.source_system
         AND s.source_service_location_id = p.source_service_location_id
         AND s.is_skipped = FALSE
         AND s.service_date::date = d.service_date::date
        LEFT JOIN technicians t ON t.id = s.technician_id
        WHERE d.pool_id = %s
          AND d.service_date >= NOW() - make_interval(days => %s)
        ORDER BY d.service_date DESC, d.id ASC
        """,
        (int(pool_id), safe_window_days),
    )
    rows = cur.fetchall()
    visits: List[Dict[str, Any]] = []
    by_service_date: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        service_date = row.get("service_date")
        key = str(service_date)
        visit = by_service_date.get(key)
        if not visit:
            visit = {
                "service_date": key,
                "technician_name": row.get("technician_name"),
                "technician_tech_id": row.get("technician_tech_id"),
                "visit_estimated_cost": 0,
                "chemicals": [],
            }
            by_service_date[key] = visit
            visits.append(visit)

        estimated_cost = row.get("estimated_cost") or 0
        try:
            visit["visit_estimated_cost"] = float(visit["visit_estimated_cost"]) + float(estimated_cost)
        except Exception:
            pass

        visit["chemicals"].append(
            {
                "description": row.get("description"),
                "dosage_key": row.get("dosage_key"),
                "unit_of_measure": row.get("unit_of_measure"),
                "quantity": row.get("quantity"),
                "entry_cost": row.get("entry_cost"),
                "estimated_cost": row.get("estimated_cost"),
            }
        )

    return visits


def _build_revenue_metadata(cur, row: Dict[str, Any], refresh_run_id: int) -> Dict[str, Any]:
    metadata = _build_metadata("revenue", row, refresh_run_id)
    rule_context = _fetch_revenue_rule_context(cur, row.get("rule_code"))
    if rule_context.get("threshold_value") is not None:
        metadata["threshold_value"] = rule_context.get("threshold_value")
    if rule_context.get("window_days") is not None:
        metadata["window_days"] = rule_context.get("window_days")
    if rule_context.get("description"):
        metadata["rule_description"] = rule_context.get("description")

    tech_context = _fetch_revenue_technician_context(cur, row.get("pool_id"))
    metadata.update(tech_context)

    visit_breakdown = _fetch_revenue_visit_breakdown(cur, row.get("pool_id"), rule_context.get("window_days"))
    if visit_breakdown:
        metadata["visit_breakdown"] = visit_breakdown

    return metadata


def _candidate_detections(cur, refresh_run_id: int) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []

    if _view_exists(cur, "current_chemistry_alerts_v"):
        cur.execute(
            """
            SELECT
                'pool' AS category,
                rule_code,
                severity,
                customer_id,
                pool_id,
                customer_name,
                pool_name,
                reading_key,
                reading_type,
                description,
                unit_of_measure,
                threshold_value,
                value,
                NULL::NUMERIC AS observed_value,
                NULL::NUMERIC AS observed_count,
                NULL::TEXT AS opportunity_type,
                service_date
            FROM current_chemistry_alerts_v
            """
        )
        for row in cur.fetchall():
            item = dict(row)
            item["entity_type"] = "pool"
            item["entity_id"] = str(item["pool_id"])
            item["metadata_json"] = _build_pool_alert_metadata(cur, "pool", item, refresh_run_id)
            candidates.append(item)

    if _view_exists(cur, "chemistry_trend_alerts_v"):
        cur.execute(
            """
            SELECT
                'process' AS category,
                rule_code,
                severity,
                customer_id,
                pool_id,
                customer_name,
                pool_name,
                reading_key,
                NULL::TEXT AS reading_type,
                NULL::TEXT AS description,
                NULL::TEXT AS unit_of_measure,
                threshold_value,
                NULL::NUMERIC AS value,
                observed_value,
                NULL::NUMERIC AS observed_count,
                NULL::TEXT AS opportunity_type,
                service_date
            FROM chemistry_trend_alerts_v
            """
        )
        for row in cur.fetchall():
            item = dict(row)
            item["entity_type"] = "pool"
            item["entity_id"] = str(item["pool_id"])
            item["metadata_json"] = _build_pool_alert_metadata(cur, "process", item, refresh_run_id)
            candidates.append(item)

    if _view_exists(cur, "revenue_opportunities_v"):
        cur.execute(
            """
            SELECT
                'revenue' AS category,
                rule_code,
                'warning' AS severity,
                customer_id,
                pool_id,
                customer_name,
                pool_name,
                reading_key,
                NULL::TEXT AS reading_type,
                NULL::TEXT AS description,
                NULL::TEXT AS unit_of_measure,
                threshold_value,
                NULL::NUMERIC AS value,
                observed_value,
                observed_count,
                opportunity_type,
                service_date
            FROM revenue_opportunities_v
            """
        )
        for row in cur.fetchall():
            item = dict(row)
            if item.get("pool_id") is not None:
                item["entity_type"] = "pool"
                item["entity_id"] = str(item["pool_id"])
            else:
                item["entity_type"] = "customer"
                item["entity_id"] = str(item["customer_id"])
            item["metadata_json"] = _build_revenue_metadata(cur, item, refresh_run_id)
            candidates.append(item)

    return candidates


def ensure_web_backend_schema() -> None:
    _ensure_backend_owned_dashboard_definitions()
    ensure_pg_schema()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_refresh_runs (
                    id BIGSERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    trigger_reason TEXT NOT NULL DEFAULT 'manual',
                    success BOOLEAN NOT NULL DEFAULT FALSE,
                    error_message TEXT,
                    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_instances (
                    id BIGSERIAL PRIMARY KEY,
                    category TEXT NOT NULL,
                    rule_code TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    pool_id BIGINT REFERENCES pools(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'open',
                    severity TEXT NOT NULL,
                    title TEXT,
                    summary TEXT,
                    first_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at TIMESTAMPTZ,
                    cleared_at TIMESTAMPTZ,
                    assigned_to TEXT,
                    acknowledged_at TIMESTAMPTZ,
                    snoozed_until TIMESTAMPTZ,
                    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (category, rule_code, entity_type, entity_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_alert_instances_status
                ON alert_instances(status, category, severity)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_alert_instances_customer
                ON alert_instances(customer_id, pool_id)
                """
            )
            cur.execute("ALTER TABLE alert_instances ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_instance_events (
                    id BIGSERIAL PRIMARY KEY,
                    alert_instance_id BIGINT NOT NULL REFERENCES alert_instances(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    event_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    actor TEXT,
                    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_alert_instance_events_alert
                ON alert_instance_events(alert_instance_id, event_ts DESC)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_instances (
                    id BIGSERIAL PRIMARY KEY,
                    source_type TEXT NOT NULL DEFAULT 'manual',
                    source_alert_instance_id BIGINT REFERENCES alert_instances(id) ON DELETE SET NULL,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    pool_id BIGINT REFERENCES pools(id) ON DELETE CASCADE,
                    technician_id BIGINT REFERENCES technicians(id) ON DELETE SET NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    title TEXT NOT NULL,
                    summary TEXT,
                    due_at TIMESTAMPTZ,
                    snoozed_until TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ,
                    canceled_at TIMESTAMPTZ,
                    assigned_to TEXT,
                    created_by TEXT,
                    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminder_instances_status
                ON reminder_instances(status, priority, due_at)
                """
            )
            cur.execute(
                """
                ALTER TABLE reminder_instances
                ADD COLUMN IF NOT EXISTS snoozed_until TIMESTAMPTZ
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminder_instances_customer
                ON reminder_instances(customer_id, pool_id, technician_id)
                """
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reminder_instances_source_alert
                ON reminder_instances(source_alert_instance_id)
                WHERE source_alert_instance_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_events (
                    id BIGSERIAL PRIMARY KEY,
                    reminder_instance_id BIGINT NOT NULL REFERENCES reminder_instances(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    event_ts TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    actor TEXT,
                    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_reminder_events_reminder
                ON reminder_events(reminder_instance_id, event_ts DESC)
                """
            )
        conn.commit()


def require_postgres_configured() -> None:
    if not DATABASE_URL:
        raise HTTPException(status_code=404, detail="DATABASE_URL is not configured")


def get_postgres_health() -> Dict[str, Any]:
    require_postgres_configured()
    try:
        row = pg_healthcheck()
        return {"ok": True, "now": row["now"] if row else None}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Postgres health check failed") from exc


def get_dashboard_summary() -> Optional[Dict[str, Any]]:
    require_postgres_configured()
    try:
        sync_filter_clean_quote_reminders()
    except Exception:
        pass
    with pg() as conn:
        with conn.cursor() as cur:
            if not _view_exists(cur, "dashboard_summary_v"):
                return None

            cur.execute("SELECT * FROM dashboard_summary_v")
            summary = cur.fetchone()
            if not summary:
                return None

            cur.execute(
                """
                SELECT category, status, COUNT(*) AS count
                FROM alert_instances
                GROUP BY category, status
                ORDER BY category, status
                """
            )
            status_counts = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT category, severity, COUNT(*) AS count
                FROM alert_instances
                WHERE status <> 'cleared'
                GROUP BY category, severity
                ORDER BY category, severity
                """
            )
            severity_counts = [dict(row) for row in cur.fetchall()]

            payload = dict(summary)
            payload["tracked_alert_counts_by_status"] = status_counts
            payload["tracked_alert_counts_by_severity"] = severity_counts
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE status IN ('open', 'acknowledged')) AS open_reminder_count,
                    COUNT(*) FILTER (
                        WHERE status IN ('open', 'acknowledged', 'snoozed')
                          AND due_at IS NOT NULL
                          AND due_at < NOW()
                    ) AS overdue_reminder_count,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed_reminder_count
                FROM reminder_instances
                """
            )
            reminder_counts = dict(cur.fetchone())
            payload["reminder_counts"] = reminder_counts
            return payload


def refresh_alert_instances(trigger_reason: str = "manual") -> Dict[str, Any]:
    require_postgres_configured()
    try:
        sync_filter_clean_quote_reminders()
    except Exception:
        pass
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO alert_refresh_runs (trigger_reason, success)
                VALUES (%s, FALSE)
                RETURNING id, started_at
                """,
                (trigger_reason,),
            )
            refresh_row = cur.fetchone()
            refresh_run_id = int(refresh_row["id"])

            try:
                candidates = _candidate_detections(cur, refresh_run_id)
                seen_keys: List[str] = []
                inserted_count = 0
                reopened_count = 0
                updated_count = 0

                for item in candidates:
                    title = _alert_title(str(item["category"]), item)
                    summary = _alert_summary(str(item["category"]), item)
                    metadata_json = _json_dumps(item["metadata_json"])
                    evidence_ts = _detection_evidence_ts(item)

                    cur.execute(
                        """
                        SELECT id, status, resolved_at
                        FROM alert_instances
                        WHERE category = %s
                          AND rule_code = %s
                          AND entity_type = %s
                          AND entity_id = %s
                        """,
                        (
                            item["category"],
                            item["rule_code"],
                            item["entity_type"],
                            item["entity_id"],
                        ),
                    )
                    existing = cur.fetchone()
                    existing_status = str(existing["status"]) if existing else ""
                    seen_key = "|".join(
                        [
                            str(item["category"]),
                            str(item["rule_code"]),
                            str(item["entity_type"]),
                            str(item["entity_id"]),
                        ]
                    )
                    seen_keys.append(seen_key)

                    if existing:
                        resolved_at = _parse_dt(existing.get("resolved_at"))
                        reopen_resolved = bool(
                            existing_status == "resolved"
                            and resolved_at is not None
                            and evidence_ts is not None
                            and evidence_ts > resolved_at
                        )
                        if existing_status == "cleared":
                            new_status = "open"
                        elif reopen_resolved:
                            new_status = "open"
                        else:
                            new_status = existing_status

                        existing_item = _fetch_alert_instance(cur, int(existing["id"])) or {}
                        previous_evidence_ts = _parse_dt((existing_item.get("metadata_json") or {}).get("service_date"))
                        severity_changed = str(existing_item.get("severity") or "") != str(item["severity"])
                        evidence_changed = evidence_ts != previous_evidence_ts
                        status_reopened = existing_status == "cleared" or reopen_resolved

                        set_clauses = [
                            "customer_id = %s",
                            "pool_id = %s",
                            "status = %s",
                            "severity = %s",
                            "title = %s",
                            "summary = %s",
                            "last_evaluated_at = NOW()",
                            "metadata_json = %s::jsonb",
                            "updated_at = NOW()",
                        ]
                        update_params: List[Any] = [
                            item.get("customer_id"),
                            item.get("pool_id"),
                            new_status,
                            item["severity"],
                            title,
                            summary,
                            metadata_json,
                        ]

                        if existing_status != "resolved" or reopen_resolved:
                            set_clauses.append("last_detected_at = NOW()")
                        if existing_status == "cleared":
                            set_clauses.append("cleared_at = NULL")
                        if reopen_resolved:
                            set_clauses.append("resolved_at = NULL")

                        cur.execute(
                            f"""
                            UPDATE alert_instances
                            SET {", ".join(set_clauses)}
                            WHERE id = %s
                            """,
                            update_params + [existing["id"]],
                        )
                        updated_count += 1
                        if status_reopened:
                            reopened_count += 1
                            cur.execute(
                                """
                                INSERT INTO alert_instance_events (
                                    alert_instance_id,
                                    event_type,
                                    actor,
                                    payload_json
                                ) VALUES (%s, %s, %s, %s::jsonb)
                                """,
                                (existing["id"], "reopened", "refresh", metadata_json),
                            )
                        elif severity_changed:
                            cur.execute(
                                """
                                INSERT INTO alert_instance_events (
                                    alert_instance_id,
                                    event_type,
                                    actor,
                                    payload_json
                                ) VALUES (%s, 'severity_changed', %s, %s::jsonb)
                                """,
                                (existing["id"], "refresh", metadata_json),
                            )
                        elif evidence_changed:
                            cur.execute(
                                """
                                INSERT INTO alert_instance_events (
                                    alert_instance_id,
                                    event_type,
                                    actor,
                                    payload_json
                                ) VALUES (%s, 'detected', %s, %s::jsonb)
                                """,
                                (existing["id"], "refresh", metadata_json),
                            )
                    else:
                        cur.execute(
                            """
                            INSERT INTO alert_instances (
                                category,
                                rule_code,
                                entity_type,
                                entity_id,
                                customer_id,
                                pool_id,
                                status,
                                severity,
                                title,
                                summary,
                                metadata_json
                            ) VALUES (%s, %s, %s, %s, %s, %s, 'open', %s, %s, %s, %s::jsonb)
                            RETURNING id
                            """,
                            (
                                item["category"],
                                item["rule_code"],
                                item["entity_type"],
                                item["entity_id"],
                                item.get("customer_id"),
                                item.get("pool_id"),
                                item["severity"],
                                title,
                                summary,
                                metadata_json,
                            ),
                        )
                        inserted = cur.fetchone()
                        inserted_count += 1
                        cur.execute(
                            """
                            INSERT INTO alert_instance_events (
                                alert_instance_id,
                                event_type,
                                actor,
                                payload_json
                            ) VALUES (%s, 'detected', %s, %s::jsonb)
                            """,
                            (inserted["id"], "refresh", metadata_json),
                        )

                cur.execute(
                    """
                    SELECT id, category, rule_code, entity_type, entity_id
                    FROM alert_instances
                    WHERE category = ANY(%s)
                      AND status <> 'cleared'
                    """,
                    (list(MANAGED_ALERT_CATEGORIES),),
                )
                to_clear = []
                seen_key_set = set(seen_keys)
                for row in cur.fetchall():
                    row_key = "|".join(
                        [
                            str(row["category"]),
                            str(row["rule_code"]),
                            str(row["entity_type"]),
                            str(row["entity_id"]),
                        ]
                    )
                    if row_key not in seen_key_set:
                        to_clear.append(int(row["id"]))

                cleared_count = 0
                for alert_id in to_clear:
                    cur.execute(
                        """
                        UPDATE alert_instances
                        SET status = 'cleared',
                            cleared_at = NOW(),
                            last_evaluated_at = NOW(),
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (alert_id,),
                    )
                    cur.execute(
                        """
                        INSERT INTO alert_instance_events (
                            alert_instance_id,
                            event_type,
                            actor,
                            payload_json
                        ) VALUES (%s, 'cleared_by_refresh', %s, %s::jsonb)
                        """,
                        (alert_id, "refresh", _json_dumps({"refresh_run_id": refresh_run_id})),
                    )
                    cleared_count += 1

                metrics = {
                    "candidate_count": len(candidates),
                    "inserted_count": inserted_count,
                    "updated_count": updated_count,
                    "reopened_count": reopened_count,
                    "cleared_count": cleared_count,
                }
                cur.execute(
                    """
                    UPDATE alert_refresh_runs
                    SET completed_at = NOW(),
                        success = TRUE,
                        metrics_json = %s::jsonb
                    WHERE id = %s
                    """,
                    (_json_dumps(metrics), refresh_run_id),
                )
                conn.commit()
                return {
                    "ok": True,
                    "refresh_run_id": refresh_run_id,
                    "metrics": metrics,
                }
            except Exception as exc:
                cur.execute(
                    """
                    UPDATE alert_refresh_runs
                    SET completed_at = NOW(),
                        success = FALSE,
                        error_message = %s
                    WHERE id = %s
                    """,
                    (str(exc), refresh_run_id),
                )
                conn.commit()
                raise


def list_alert_instances(
    *,
    status: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    rule_code: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    require_postgres_configured()
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    filters: List[str] = []
    params: List[Any] = []

    normalized_status = _normalize_status(status)
    normalized_category = _normalize_status(category)
    normalized_severity = _normalize_status(severity)
    normalized_rule_code = (rule_code or "").strip()
    search_value = (search or "").strip()

    if normalized_status:
        if normalized_status not in ACTIONABLE_ALERT_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unsupported status '{normalized_status}'")
        filters.append("status = %s")
        params.append(normalized_status)
    else:
        filters.append("status <> 'cleared'")
    if normalized_category:
        filters.append("category = %s")
        params.append(normalized_category)
    if normalized_severity:
        filters.append("severity = %s")
        params.append(normalized_severity)
    if normalized_rule_code:
        filters.append("rule_code = %s")
        params.append(normalized_rule_code)
    if search_value:
        filters.append(
            """
            (
                title ILIKE %s
                OR summary ILIKE %s
                OR rule_code ILIKE %s
                OR COALESCE(metadata_json->>'customer_name', '') ILIKE %s
                OR COALESCE(metadata_json->>'pool_name', '') ILIKE %s
                OR COALESCE(metadata_json->>'opportunity_type', '') ILIKE %s
            )
            """
        )
        pattern = f"%{search_value}%"
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])

    where_sql = ""
    if filters:
        where_sql = "WHERE " + " AND ".join(filters)

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM alert_instances
                {where_sql}
                """,
                params,
            )
            total = int(cur.fetchone()["total"])

            cur.execute(
                f"""
                SELECT
                    id,
                    category,
                    rule_code,
                    entity_type,
                    entity_id,
                    customer_id,
                    pool_id,
                    status,
                    severity,
                    title,
                    summary,
                    first_detected_at,
                    last_detected_at,
                    last_evaluated_at,
                    resolved_at,
                    cleared_at,
                    assigned_to,
                    acknowledged_at,
                    snoozed_until,
                    metadata_json
                FROM alert_instances
                {where_sql}
                ORDER BY
                    CASE status
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        WHEN 'snoozed' THEN 2
                        WHEN 'resolved' THEN 3
                        WHEN 'cleared' THEN 4
                        ELSE 5
                    END,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    last_detected_at DESC,
                    id DESC
                LIMIT %s OFFSET %s
                """,
                params + [safe_limit, safe_offset],
            )
            rows = [dict(row) for row in cur.fetchall()]

    return {
        "ok": True,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "items": rows,
        "filters": {
            "status": normalized_status,
            "category": normalized_category,
            "rule_code": normalized_rule_code or None,
            "search": search_value or None,
        },
    }


def get_alert_instance(alert_id: int) -> Dict[str, Any]:
    require_postgres_configured()
    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_alert_instance(cur, int(alert_id))
            if not item:
                raise HTTPException(status_code=404, detail="Alert not found")

            chart_policy = _load_customer_chart_policy()
            metadata = item.get("metadata_json") or {}
            reading_key = metadata.get("reading_key")
            alert_chart_keys: List[str] = []
            if reading_key:
                alert_chart_keys.append(str(reading_key))
            if str(reading_key or "") == "fc_cya_ratio":
                alert_chart_keys = ["free_chlorine", "cya"]

            cur.execute(
                """
                SELECT id, event_type, event_ts, actor, payload_json
                FROM alert_instance_events
                WHERE alert_instance_id = %s
                ORDER BY event_ts DESC, id DESC
                LIMIT 100
                """,
                (int(alert_id),),
            )
            events = [dict(row) for row in cur.fetchall()]

            chemistry_history: List[Dict[str, Any]] = []
            customer_id = item.get("customer_id")
            if customer_id is not None:
                params: List[Any] = [int(customer_id)]
                pool_filter = ""
                if item.get("pool_id") is not None:
                    pool_filter = "AND r.pool_id = %s"
                    params.append(int(item["pool_id"]))
                cur.execute(
                    f"""
                    SELECT
                        r.pool_id,
                        p.name AS pool_name,
                        r.reading_key,
                        r.reading_type,
                        r.description,
                        r.unit_of_measure,
                        r.service_date,
                        r.value,
                        r.raw_json
                    FROM chemistry_readings r
                    LEFT JOIN pools p ON p.id = r.pool_id
                    WHERE r.customer_id = %s
                      {pool_filter}
                      AND r.service_date >= NOW() - INTERVAL '365 days'
                    ORDER BY r.pool_id ASC, r.reading_key ASC, r.service_date ASC, r.id ASC
                    """,
                    params,
                )
                chemistry_history = [dict(row) for row in cur.fetchall()]

    return {
        "ok": True,
        "item": item,
        "events": events,
        "chemistry_history": chemistry_history,
        "chart_policy": chart_policy,
        "alert_chart_keys": alert_chart_keys,
    }


def list_alert_events(alert_id: int, limit: int = 100) -> Dict[str, Any]:
    require_postgres_configured()
    safe_limit = max(1, min(int(limit), 200))
    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_alert_instance(cur, int(alert_id))
            if not item:
                raise HTTPException(status_code=404, detail="Alert not found")

            cur.execute(
                """
                SELECT id, event_type, event_ts, actor, payload_json
                FROM alert_instance_events
                WHERE alert_instance_id = %s
                ORDER BY event_ts DESC, id DESC
                LIMIT %s
                """,
                (int(alert_id), safe_limit),
            )
            events = [dict(row) for row in cur.fetchall()]

    return {"ok": True, "alert_id": int(alert_id), "items": events, "limit": safe_limit}


def list_refresh_runs(limit: int = 20) -> Dict[str, Any]:
    require_postgres_configured()
    safe_limit = max(1, min(int(limit), 100))
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, started_at, completed_at, trigger_reason, success, error_message, metrics_json
                FROM alert_refresh_runs
                ORDER BY id DESC
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    return {"ok": True, "items": rows, "limit": safe_limit}


def get_refresh_run(refresh_run_id: int) -> Dict[str, Any]:
    require_postgres_configured()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, started_at, completed_at, trigger_reason, success, error_message, metrics_json
                FROM alert_refresh_runs
                WHERE id = %s
                """,
                (int(refresh_run_id),),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Refresh run not found")
    return {"ok": True, "item": dict(row)}


def list_customers(
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    operational_only: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    require_postgres_configured()
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    filters: List[str] = []
    params: List[Any] = []

    normalized_status = _normalize_status(status)
    if normalized_status:
        filters.append("lower(c.customer_status) = %s")
        params.append(normalized_status)

    if operational_only:
        filters.append("c.is_operationally_active = TRUE")

    search_value = (search or "").strip()
    if search_value:
        filters.append(
            """
            (
                c.first_name ILIKE %s
                OR c.last_name ILIKE %s
                OR c.company_name ILIKE %s
                OR c.email ILIKE %s
                OR c.phone ILIKE %s
                OR c.mobile_phone ILIKE %s
            )
            """
        )
        pattern = f"%{search_value}%"
        params.extend([pattern, pattern, pattern, pattern, pattern, pattern])

    where_sql = ""
    if filters:
        where_sql = "WHERE " + " AND ".join(filters)

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM customers c
                {where_sql}
                """,
                params,
            )
            total = int(cur.fetchone()["total"])

            cur.execute(
                f"""
                SELECT
                    c.id,
                    c.source_system,
                    c.source_customer_id,
                    c.first_name,
                    c.last_name,
                    c.company_name,
                    c.email,
                    c.phone,
                    c.mobile_phone,
                    c.city,
                    c.state,
                    c.customer_status,
                    c.is_lead,
                    c.is_inactive,
                    c.is_operationally_active,
                    c.inactive_since,
                    c.has_pool,
                    c.pool_count,
                    c.created_at,
                    c.updated_at,
                    COALESCE(alerts.open_alert_count, 0) AS open_alert_count,
                    COALESCE(alerts.critical_alert_count, 0) AS critical_alert_count
                FROM customers c
                LEFT JOIN (
                    SELECT
                        customer_id,
                        COUNT(*) FILTER (WHERE status <> 'cleared') AS open_alert_count,
                        COUNT(*) FILTER (WHERE status <> 'cleared' AND severity = 'critical') AS critical_alert_count
                    FROM alert_instances
                    GROUP BY customer_id
                ) alerts ON alerts.customer_id = c.id
                {where_sql}
                ORDER BY
                    c.is_operationally_active DESC,
                    COALESCE(c.company_name, '') = '' ASC,
                    COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), c.company_name, c.source_customer_id) ASC,
                    c.id ASC
                LIMIT %s OFFSET %s
                """,
                params + [safe_limit, safe_offset],
            )
            items = [dict(row) for row in cur.fetchall()]

    return {
        "ok": True,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "items": items,
    }


def get_customer_detail(customer_id: int) -> Dict[str, Any]:
    require_postgres_configured()
    chart_policy = _load_customer_chart_policy()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    c.*,
                    COALESCE(alerts.open_alert_count, 0) AS open_alert_count,
                    COALESCE(alerts.critical_alert_count, 0) AS critical_alert_count
                FROM customers c
                LEFT JOIN (
                    SELECT
                        customer_id,
                        COUNT(*) FILTER (WHERE status <> 'cleared') AS open_alert_count,
                        COUNT(*) FILTER (WHERE status <> 'cleared' AND severity = 'critical') AS critical_alert_count
                    FROM alert_instances
                    GROUP BY customer_id
                ) alerts ON alerts.customer_id = c.id
                WHERE c.id = %s
                """,
                (int(customer_id),),
            )
            customer = cur.fetchone()
            if not customer:
                raise HTTPException(status_code=404, detail="Customer not found")

            cur.execute(
                """
                SELECT
                    id,
                    source_pool_id,
                    name,
                    gallons,
                    baseline_filter_pressure,
                    address,
                    city,
                    state,
                    zip,
                    is_operationally_active,
                    created_at,
                    updated_at
                FROM pools
                WHERE customer_id = %s
                ORDER BY id ASC
                """,
                (int(customer_id),),
            )
            pools = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT
                    t.id AS technician_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''),
                        NULLIF(t.email, ''),
                        t.id::text
                    ) AS technician_name,
                    t.role_type,
                    a.day_of_week,
                    a.frequency
                FROM service_location_technician_assignments a
                JOIN technicians t ON t.id = a.technician_id
                WHERE a.customer_id = %s
                  AND a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                ORDER BY technician_name ASC, a.day_of_week ASC NULLS LAST, a.frequency ASC NULLS LAST
                """,
                (int(customer_id),),
            )
            assigned_technicians = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    id,
                    category,
                    rule_code,
                    entity_type,
                    entity_id,
                    pool_id,
                    status,
                    severity,
                    title,
                    summary,
                    first_detected_at,
                    last_detected_at,
                    last_evaluated_at,
                    resolved_at,
                    cleared_at
                FROM alert_instances
                WHERE customer_id = %s
                  AND status <> 'cleared'
                ORDER BY
                    CASE status
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        WHEN 'snoozed' THEN 2
                        WHEN 'resolved' THEN 3
                        WHEN 'cleared' THEN 4
                        ELSE 5
                    END,
                    CASE severity
                        WHEN 'critical' THEN 0
                        WHEN 'warning' THEN 1
                        ELSE 2
                    END,
                    id DESC
                LIMIT 200
                """,
                (int(customer_id),),
            )
            alerts = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    r.id,
                    r.status,
                    r.priority,
                    r.title,
                    r.summary,
                    r.due_at,
                    r.snoozed_until,
                    r.completed_at,
                    r.assigned_to,
                    r.source_type,
                    r.source_alert_instance_id
                FROM reminder_instances r
                WHERE r.customer_id = %s
                ORDER BY
                    CASE r.status
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        WHEN 'snoozed' THEN 2
                        WHEN 'completed' THEN 3
                        WHEN 'canceled' THEN 4
                        ELSE 5
                    END,
                    r.due_at ASC NULLS LAST,
                    r.id DESC
                LIMIT 100
                """,
                (int(customer_id),),
            )
            reminders = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    MAX(service_date) AS latest_service_date
                FROM chemistry_readings
                WHERE customer_id = %s
                """,
                (int(customer_id),),
            )
            chemistry_summary = cur.fetchone()

            cur.execute(
                """
                SELECT
                    r.pool_id,
                    p.name AS pool_name,
                    r.reading_key,
                    r.reading_type,
                    r.description,
                    r.unit_of_measure,
                    r.service_date,
                    r.value,
                    r.raw_json
                FROM chemistry_readings r
                LEFT JOIN pools p ON p.id = r.pool_id
                WHERE r.customer_id = %s
                  AND r.service_date >= NOW() - INTERVAL '365 days'
                ORDER BY r.pool_id ASC, r.reading_key ASC, r.service_date ASC, r.id ASC
                """,
                (int(customer_id),),
            )
            chemistry_history = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    COALESCE(SUM(d.estimated_cost) FILTER (WHERE d.service_date >= NOW() - INTERVAL '30 days'), 0) AS cost_30d,
                    COALESCE(SUM(d.estimated_cost) FILTER (WHERE d.service_date >= NOW() - INTERVAL '60 days'), 0) AS cost_60d,
                    COALESCE(SUM(d.estimated_cost) FILTER (WHERE d.service_date >= NOW() - INTERVAL '90 days'), 0) AS cost_90d,
                    COALESCE(SUM(d.estimated_cost), 0) AS lifetime_cost,
                    MAX(d.service_date) AS latest_dose_date,
                    COUNT(*) AS total_dose_events
                FROM chemical_dose_events d
                WHERE d.customer_id = %s
                """,
                (int(customer_id),),
            )
            chemical_spend_summary = dict(cur.fetchone() or {})

            cur.execute(
                """
                SELECT
                    d.pool_id,
                    p.name AS pool_name,
                    d.service_date,
                    COALESCE(SUM(d.estimated_cost), 0) AS visit_estimated_cost,
                    jsonb_agg(
                        jsonb_build_object(
                            'description', d.description,
                            'dosage_key', d.dosage_key,
                            'quantity', d.quantity,
                            'unit_of_measure', d.unit_of_measure,
                            'entry_cost', d.entry_cost,
                            'estimated_cost', d.estimated_cost
                        )
                        ORDER BY d.description ASC, d.id ASC
                    ) AS chemicals
                FROM chemical_dose_events d
                LEFT JOIN pools p ON p.id = d.pool_id
                WHERE d.customer_id = %s
                  AND d.service_date >= NOW() - INTERVAL '120 days'
                GROUP BY d.pool_id, p.name, d.service_date
                ORDER BY d.service_date DESC, d.pool_id ASC
                LIMIT 120
                """,
                (int(customer_id),),
            )
            chemical_spend_by_visit = [dict(row) for row in cur.fetchall()]

    return {
        "ok": True,
        "item": dict(customer),
        "pools": pools,
        "assigned_technicians": assigned_technicians,
        "alerts": alerts,
        "reminders": reminders,
        "latest_chemistry_service_date": chemistry_summary["latest_service_date"] if chemistry_summary else None,
        "chemistry_history": chemistry_history,
        "chart_policy": chart_policy,
        "chemical_spend_summary": chemical_spend_summary,
        "chemical_spend_by_visit": chemical_spend_by_visit,
    }


def list_reminders(
    *,
    status: Optional[str] = None,
    assigned_to: Optional[str] = None,
    source_type: Optional[str] = None,
    overdue_only: bool = False,
    search: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    require_postgres_configured()
    try:
        sync_filter_clean_quote_reminders()
    except Exception:
        pass
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    params: List[Any] = []
    where_clauses: List[str] = []

    normalized_status = _normalize_status(status)
    if normalized_status:
        where_clauses.append("r.status = %s")
        params.append(normalized_status)

    assigned_to_value = (assigned_to or "").strip()
    if assigned_to_value:
        where_clauses.append("COALESCE(r.assigned_to, '') = %s")
        params.append(assigned_to_value)

    source_type_value = (source_type or "").strip()
    if source_type_value:
        where_clauses.append("COALESCE(r.source_type, '') = %s")
        params.append(source_type_value)

    if overdue_only:
        where_clauses.append(
            "r.status IN ('open', 'acknowledged', 'snoozed') AND r.due_at IS NOT NULL AND r.due_at < NOW()"
        )

    search_value = (search or "").strip()
    if search_value:
        pattern = f"%{search_value}%"
        where_clauses.append(
            """
            (
                r.title ILIKE %s
                OR r.summary ILIKE %s
                OR COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), c.company_name, '') ILIKE %s
                OR p.name ILIKE %s
                OR COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), t.username, '') ILIKE %s
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    from_sql = f"""
        FROM reminder_instances r
        LEFT JOIN customers c ON c.id = r.customer_id
        LEFT JOIN pools p ON p.id = r.pool_id
        LEFT JOIN technicians t ON t.id = r.technician_id
        LEFT JOIN alert_instances a ON a.id = r.source_alert_instance_id
        {where_sql}
    """

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total " + from_sql, params)
            total = int(cur.fetchone()["total"])

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE r.status IN ('open', 'acknowledged')) AS actionable_count,
                    COUNT(*) FILTER (
                        WHERE r.status IN ('open', 'acknowledged', 'snoozed')
                          AND r.due_at IS NOT NULL
                          AND r.due_at < NOW()
                    ) AS overdue_count,
                    COUNT(*) FILTER (WHERE r.status = 'completed') AS completed_count,
                    COUNT(*) FILTER (WHERE r.source_alert_instance_id IS NOT NULL) AS linked_alert_count
                """
                + from_sql,
                params,
            )
            summary = dict(cur.fetchone())

            cur.execute(
                """
                SELECT
                    r.id,
                    r.source_type,
                    r.source_alert_instance_id,
                    r.customer_id,
                    r.pool_id,
                    r.technician_id,
                    r.status,
                    r.priority,
                    r.title,
                    r.summary,
                    r.due_at,
                    r.snoozed_until,
                    r.completed_at,
                    r.canceled_at,
                    r.assigned_to,
                    r.created_by,
                    r.created_at,
                    r.updated_at,
                    a.category AS source_alert_category,
                    a.rule_code AS source_alert_rule_code,
                    a.severity AS source_alert_severity,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                        NULLIF(c.company_name, ''),
                        NULL
                    ) AS customer_name,
                    p.name AS pool_name,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''),
                        NULLIF(t.username, ''),
                        NULL
                    ) AS technician_name
                """
                + from_sql
                + """
                ORDER BY
                    CASE r.status
                        WHEN 'open' THEN 0
                        WHEN 'acknowledged' THEN 1
                        WHEN 'snoozed' THEN 2
                        WHEN 'completed' THEN 3
                        WHEN 'canceled' THEN 4
                        ELSE 5
                    END,
                    CASE r.priority
                        WHEN 'high' THEN 0
                        WHEN 'normal' THEN 1
                        WHEN 'low' THEN 2
                        ELSE 3
                    END,
                    r.due_at ASC NULLS LAST,
                    r.id DESC
                LIMIT %s OFFSET %s
                """,
                params + [safe_limit, safe_offset],
            )
            items = [dict(row) for row in cur.fetchall()]

    return {
        "ok": True,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "items": items,
        "summary": summary,
        "filters": {
            "status": normalized_status,
            "assigned_to": assigned_to_value or None,
            "source_type": source_type_value or None,
            "overdue_only": bool(overdue_only),
            "search": search_value or None,
        },
        "source": "reminder_instances",
    }


def get_reminder_detail(reminder_id: int) -> Dict[str, Any]:
    require_postgres_configured()
    try:
        sync_filter_clean_quote_reminders(reminder_ids=[int(reminder_id)])
    except Exception:
        pass
    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_reminder_instance(cur, int(reminder_id))
            if not item:
                raise HTTPException(status_code=404, detail="Reminder not found")

            cur.execute(
                """
                SELECT
                    id,
                    event_type,
                    event_ts,
                    actor,
                    payload_json
                FROM reminder_events
                WHERE reminder_instance_id = %s
                ORDER BY event_ts DESC, id DESC
                LIMIT 100
                """,
                (int(reminder_id),),
            )
            events = [dict(row) for row in cur.fetchall()]

            linked_alert = None
            if item.get("source_alert_instance_id") is not None:
                linked_alert = _fetch_alert_instance(cur, int(item["source_alert_instance_id"]))

    return {
        "ok": True,
        "item": item,
        "events": events,
        "linked_alert": linked_alert,
        "source": "reminder_instances",
    }


def create_alert_reminder(
    alert_id: int,
    *,
    actor: str,
    due_at: Optional[str] = None,
    assigned_to: Optional[str] = None,
    title: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    with pg() as conn:
        with conn.cursor() as cur:
            alert = _fetch_alert_instance(cur, int(alert_id))
            if not alert:
                raise HTTPException(status_code=404, detail="Alert not found")

            cur.execute(
                """
                SELECT id
                FROM reminder_instances
                WHERE source_alert_instance_id = %s
                """,
                (int(alert_id),),
            )
            existing = cur.fetchone()
            if existing:
                item = _fetch_reminder_instance(cur, int(existing["id"]))
                return {"ok": True, "created": False, "item": item}

            due_at_dt = _parse_dt(due_at)
            assigned_to_value = (assigned_to or "").strip() or None
            note_value = (note or "").strip() or None
            title_value = (title or "").strip() or f"Follow up: {alert.get('title') or 'alert'}"
            summary_value = note_value or alert.get("summary")
            priority = "high" if str(alert.get("severity") or "").lower() == "critical" else "normal"
            metadata = {
                "created_from_alert_id": int(alert_id),
                "source_alert_category": alert.get("category"),
                "source_alert_rule_code": alert.get("rule_code"),
                "source_alert_status": alert.get("status"),
            }
            if note_value:
                metadata["note"] = note_value

            cur.execute(
                """
                INSERT INTO reminder_instances (
                    source_type,
                    source_alert_instance_id,
                    customer_id,
                    pool_id,
                    status,
                    priority,
                    title,
                    summary,
                    due_at,
                    snoozed_until,
                    assigned_to,
                    created_by,
                    metadata_json
                ) VALUES (%s, %s, %s, %s, 'open', %s, %s, %s, %s, NULL, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    "alert",
                    int(alert_id),
                    alert.get("customer_id"),
                    alert.get("pool_id"),
                    priority,
                    title_value[:250],
                    summary_value[:2000] if summary_value else None,
                    due_at_dt,
                    assigned_to_value,
                    actor,
                    _json_dumps(metadata),
                ),
            )
            reminder_id = int(cur.fetchone()["id"])
            cur.execute(
                """
                INSERT INTO reminder_events (
                    reminder_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    reminder_id,
                    "created_from_alert",
                    actor,
                    _json_dumps(
                        {
                            "alert_id": int(alert_id),
                            "assigned_to": assigned_to_value,
                            "due_at": due_at_dt,
                            "note": note_value,
                        }
                    ),
                ),
            )
            conn.commit()
            item = _fetch_reminder_instance(cur, reminder_id)

    return {"ok": True, "created": True, "item": item}


def _fetch_filter_clean_alert_context(cur, alert: Dict[str, Any]) -> Dict[str, Any]:
    cur.execute(
        """
        SELECT
            a.id AS alert_id,
            a.rule_code,
            a.status AS alert_status,
            a.customer_id,
            a.pool_id,
            c.source_customer_id,
            c.phone,
            c.mobile_phone,
            c.email,
            COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), NULLIF(c.company_name, ''), NULL) AS customer_name,
            p.name AS pool_name,
            p.source_service_location_id,
            sc.ghl_contact_id
        FROM alert_instances a
        LEFT JOIN customers c ON c.id = a.customer_id
        LEFT JOIN pools p ON p.id = a.pool_id
        LEFT JOIN sk_customer sc
          ON sc.source_system = c.source_system
         AND sc.source_customer_id = c.source_customer_id
        WHERE a.id = %s
        """,
        (int(alert["id"]),),
    )
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Alert context not found")
    return dict(row)


def _upsert_filter_clean_quote_reminder(
    cur,
    *,
    alert: Dict[str, Any],
    context: Dict[str, Any],
    actor: str,
    assigned_to: Optional[str],
    due_at: Optional[datetime],
    note: Optional[str],
) -> Dict[str, Any]:
    reminder_title = _filter_clean_reminder_title(alert)
    reminder_summary = _filter_clean_reminder_summary(alert, note)
    assigned_to_value = (assigned_to or "").strip() or None
    metadata = {
        "workflow": FILTER_CLEAN_QUOTE_REMINDER_SOURCE,
        "created_from_alert_id": int(alert["id"]),
        "source_alert_category": alert.get("category"),
        "source_alert_rule_code": alert.get("rule_code"),
        "source_alert_status": alert.get("status"),
        "source_customer_id": context.get("source_customer_id"),
        "source_service_location_id": context.get("source_service_location_id"),
        "ghl_contact_id": context.get("ghl_contact_id"),
        "customer_name": context.get("customer_name"),
        "pool_name": context.get("pool_name"),
    }
    if note:
        metadata["note"] = str(note).strip()[:2000]

    cur.execute(
        """
        SELECT id
        FROM reminder_instances
        WHERE source_alert_instance_id = %s
        """,
        (int(alert["id"]),),
    )
    existing = cur.fetchone()
    if existing:
        current = _fetch_reminder_instance(cur, int(existing["id"])) or {}
        merged_metadata = dict(current.get("metadata_json") or {})
        merged_metadata.update(metadata)
        cur.execute(
            """
            UPDATE reminder_instances
            SET
                source_type = %s,
                customer_id = %s,
                pool_id = %s,
                status = 'open',
                priority = 'normal',
                title = %s,
                summary = %s,
                due_at = %s,
                snoozed_until = NULL,
                completed_at = NULL,
                canceled_at = NULL,
                assigned_to = %s,
                metadata_json = %s::jsonb,
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                FILTER_CLEAN_QUOTE_REMINDER_SOURCE,
                alert.get("customer_id"),
                alert.get("pool_id"),
                reminder_title,
                reminder_summary,
                due_at,
                assigned_to_value if assigned_to is not None else current.get("assigned_to"),
                _json_dumps(merged_metadata),
                int(existing["id"]),
            ),
        )
        reminder_id = int(existing["id"])
        cur.execute(
            """
            INSERT INTO reminder_events (
                reminder_instance_id,
                event_type,
                actor,
                payload_json
            ) VALUES (%s, %s, %s, %s::jsonb)
            """,
            (
                reminder_id,
                "quote_notify_upserted",
                actor,
                _json_dumps(
                    {
                        "assigned_to": assigned_to_value,
                        "due_at": due_at,
                        "note": note,
                    }
                ),
            ),
        )
        return _fetch_reminder_instance(cur, reminder_id) or {}

    cur.execute(
        """
        INSERT INTO reminder_instances (
            source_type,
            source_alert_instance_id,
            customer_id,
            pool_id,
            status,
            priority,
            title,
            summary,
            due_at,
            snoozed_until,
            assigned_to,
            created_by,
            metadata_json
        ) VALUES (%s, %s, %s, %s, 'open', 'normal', %s, %s, %s, NULL, %s, %s, %s::jsonb)
        RETURNING id
        """,
        (
            FILTER_CLEAN_QUOTE_REMINDER_SOURCE,
            int(alert["id"]),
            alert.get("customer_id"),
            alert.get("pool_id"),
            reminder_title,
            reminder_summary,
            due_at,
            assigned_to_value,
            actor,
            _json_dumps(metadata),
        ),
    )
    reminder_id = int(cur.fetchone()["id"])
    cur.execute(
        """
        INSERT INTO reminder_events (
            reminder_instance_id,
            event_type,
            actor,
            payload_json
        ) VALUES (%s, %s, %s, %s::jsonb)
        """,
        (
            reminder_id,
            "quote_notify_created",
            actor,
            _json_dumps(
                {
                    "assigned_to": assigned_to_value,
                    "due_at": due_at,
                    "note": note,
                }
            ),
        ),
    )
    return _fetch_reminder_instance(cur, reminder_id) or {}


def _record_filter_clean_notification_result(
    *,
    alert_id: int,
    reminder_id: int,
    actor: str,
    sms_body: str,
    notification_sent: bool,
    notification_error: Optional[str],
    conversation_id: Optional[str],
    quote_match: Optional[Dict[str, Any]],
    quote_source: Optional[str],
) -> None:
    with pg() as conn:
        with conn.cursor() as cur:
            reminder = _fetch_reminder_instance(cur, int(reminder_id))
            merged_metadata = dict((reminder or {}).get("metadata_json") or {})
            merged_metadata["last_notification_sms"] = sms_body
            merged_metadata["last_notification_sent_at"] = _now_tz()
            merged_metadata["last_notification_sent"] = bool(notification_sent)
            if conversation_id:
                merged_metadata["last_notification_conversation_id"] = conversation_id
            if notification_error:
                merged_metadata["last_notification_error"] = notification_error
            else:
                merged_metadata.pop("last_notification_error", None)
            if quote_match:
                merged_metadata["quote_detected"] = {
                    "quote_id": _extract_quote_identifier(quote_match),
                    "source": quote_source,
                    "detected_at": _now_tz(),
                }

            cur.execute(
                """
                UPDATE reminder_instances
                SET metadata_json = %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (_json_dumps(merged_metadata), int(reminder_id)),
            )
            cur.execute(
                """
                INSERT INTO reminder_events (
                    reminder_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    int(reminder_id),
                    "customer_notified" if notification_sent else "customer_notify_failed",
                    actor,
                    _json_dumps(
                        {
                            "conversation_id": conversation_id,
                            "notification_error": notification_error,
                        }
                    ),
                ),
            )
            cur.execute(
                """
                INSERT INTO alert_instance_events (
                    alert_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (
                    int(alert_id),
                    "customer_notified" if notification_sent else "customer_notify_failed",
                    actor,
                    _json_dumps(
                        {
                            "reminder_id": int(reminder_id),
                            "conversation_id": conversation_id,
                            "notification_error": notification_error,
                        }
                    ),
                ),
            )
            conn.commit()


def sync_filter_clean_quote_reminders(
    *,
    reminder_ids: Optional[List[int]] = None,
    actor: str = "quote_sync",
) -> Dict[str, Any]:
    require_postgres_configured()
    reminder_filters = ""
    params: List[Any] = [FILTER_CLEAN_QUOTE_REMINDER_SOURCE]
    if reminder_ids:
        reminder_filters = "AND r.id = ANY(%s)"
        params.append([int(reminder_id) for reminder_id in reminder_ids])

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    r.id,
                    r.status,
                    r.source_alert_instance_id,
                    r.metadata_json,
                    c.source_customer_id,
                    p.source_service_location_id
                FROM reminder_instances r
                LEFT JOIN customers c ON c.id = r.customer_id
                LEFT JOIN pools p ON p.id = r.pool_id
                WHERE r.source_type = %s
                  AND r.status IN ('open', 'acknowledged', 'snoozed')
                  {reminder_filters}
                ORDER BY r.id ASC
                """,
                params,
            )
            reminder_rows = [dict(row) for row in cur.fetchall()]

    if not reminder_rows:
        return {"ok": True, "checked": 0, "completed": 0}

    quotes, quote_source = _fetch_skimmer_quotes()
    completed = 0
    checked = 0

    with pg() as conn:
        with conn.cursor() as cur:
            for row in reminder_rows:
                metadata = row.get("metadata_json") or {}
                source_customer_id = str(
                    metadata.get("source_customer_id") or row.get("source_customer_id") or ""
                ).strip() or None
                source_service_location_id = str(
                    metadata.get("source_service_location_id") or row.get("source_service_location_id") or ""
                ).strip() or None
                if not source_customer_id and not source_service_location_id:
                    continue
                checked += 1
                quote_match = _find_matching_filter_clean_quote(
                    quotes,
                    source_customer_id=source_customer_id,
                    source_service_location_id=source_service_location_id,
                )
                if not quote_match:
                    continue

                merged_metadata = dict(metadata)
                merged_metadata["quote_detected"] = {
                    "quote_id": _extract_quote_identifier(quote_match),
                    "source": quote_source,
                    "detected_at": _now_tz(),
                }
                cur.execute(
                    """
                    UPDATE reminder_instances
                    SET
                        status = 'completed',
                        completed_at = NOW(),
                        snoozed_until = NULL,
                        updated_at = NOW(),
                        metadata_json = %s::jsonb
                    WHERE id = %s
                    """,
                    (_json_dumps(merged_metadata), int(row["id"])),
                )
                cur.execute(
                    """
                    INSERT INTO reminder_events (
                        reminder_instance_id,
                        event_type,
                        actor,
                        payload_json
                    ) VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    (
                        int(row["id"]),
                        "completed_quote_detected",
                        actor,
                        _json_dumps(
                            {
                                "quote_id": _extract_quote_identifier(quote_match),
                                "quote_source": quote_source,
                            }
                        ),
                    ),
                )
                if row.get("source_alert_instance_id") is not None:
                    cur.execute(
                        """
                        INSERT INTO alert_instance_events (
                            alert_instance_id,
                            event_type,
                            actor,
                            payload_json
                        ) VALUES (%s, %s, %s, %s::jsonb)
                        """,
                        (
                            int(row["source_alert_instance_id"]),
                            "quote_detected",
                            actor,
                            _json_dumps(
                                {
                                    "reminder_id": int(row["id"]),
                                    "quote_id": _extract_quote_identifier(quote_match),
                                    "quote_source": quote_source,
                                }
                            ),
                        ),
                    )
                completed += 1
            conn.commit()

    return {"ok": True, "checked": checked, "completed": completed, "quote_source": quote_source}


def notify_filter_clean_customer(
    alert_id: int,
    *,
    actor: str,
    due_at: Optional[str] = None,
    assigned_to: Optional[str] = None,
    note: Optional[str] = None,
    sms_body: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    due_at_dt = _parse_dt(due_at) or _default_filter_clean_quote_due_at()
    sms_body_value = (sms_body or "").strip() or DEFAULT_FILTER_CLEAN_NOTIFY_SMS
    note_value = (note or "").strip() or None

    with pg() as conn:
        with conn.cursor() as cur:
            alert = _fetch_alert_instance(cur, int(alert_id))
            if not alert:
                raise HTTPException(status_code=404, detail="Alert not found")
            if str(alert.get("rule_code") or "").strip() not in FILTER_CLEAN_RULE_CODES:
                raise HTTPException(status_code=400, detail="Notify customer is only supported for filter-clean alerts")

            context = _fetch_filter_clean_alert_context(cur, alert)
            reminder = _upsert_filter_clean_quote_reminder(
                cur,
                alert=alert,
                context=context,
                actor=actor,
                assigned_to=assigned_to,
                due_at=due_at_dt,
                note=note_value,
            )
            conn.commit()

    notification_sent = False
    notification_error: Optional[str] = None
    conversation_id: Optional[str] = None
    try:
        ghl_contact_id = str(context.get("ghl_contact_id") or "").strip()
        phone = str(context.get("mobile_phone") or context.get("phone") or "").strip()
        if not ghl_contact_id:
            notification_error = "Customer has no linked GHL contact id."
        else:
            conversation_id = _ghl_find_conversation_id(ghl_contact_id, phone)
            if not conversation_id:
                notification_error = "No GHL conversation found for customer."
            else:
                _ghl_send_sms(conversation_id, ghl_contact_id, sms_body_value)
                notification_sent = True
    except Exception as exc:
        notification_error = str(getattr(exc, "detail", exc))[:300]

    quotes, quote_source = _fetch_skimmer_quotes()
    quote_match = _find_matching_filter_clean_quote(
        quotes,
        source_customer_id=context.get("source_customer_id"),
        source_service_location_id=context.get("source_service_location_id"),
    )
    _record_filter_clean_notification_result(
        alert_id=int(alert_id),
        reminder_id=int(reminder["id"]),
        actor=actor,
        sms_body=sms_body_value,
        notification_sent=notification_sent,
        notification_error=notification_error,
        conversation_id=conversation_id,
        quote_match=quote_match,
        quote_source=quote_source,
    )

    sync_filter_clean_quote_reminders(reminder_ids=[int(reminder["id"])])

    with pg() as conn:
        with conn.cursor() as cur:
            updated = _fetch_reminder_instance(cur, int(reminder["id"]))

    return {
        "ok": True,
        "item": updated,
        "notification_sent": notification_sent,
        "notification_error": notification_error,
        "conversation_id": conversation_id,
        "quote_detected": bool(quote_match),
        "quote_id": _extract_quote_identifier(quote_match) if quote_match else None,
        "quote_source": quote_source,
    }


def update_reminder_status(
    reminder_id: int,
    *,
    next_status: str,
    actor: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    normalized_status = _normalize_status(next_status)
    if normalized_status not in ("acknowledged", "completed", "canceled"):
        raise HTTPException(status_code=400, detail=f"Unsupported next status '{next_status}'")

    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_reminder_instance(cur, int(reminder_id))
            if not item:
                raise HTTPException(status_code=404, detail="Reminder not found")

            updates: List[str] = ["status = %s", "updated_at = NOW()"]
            params: List[Any] = [normalized_status]
            event_type = normalized_status
            if normalized_status == "acknowledged":
                updates.append("completed_at = NULL")
                updates.append("canceled_at = NULL")
                updates.append("snoozed_until = NULL")
            elif normalized_status == "completed":
                updates.append("completed_at = NOW()")
                updates.append("canceled_at = NULL")
                updates.append("snoozed_until = NULL")
            elif normalized_status == "canceled":
                updates.append("canceled_at = NOW()")
                updates.append("completed_at = NULL")
                updates.append("snoozed_until = NULL")

            params.append(int(reminder_id))
            cur.execute(
                f"""
                UPDATE reminder_instances
                SET {", ".join(updates)}
                WHERE id = %s
                """,
                params,
            )

            payload = {"status": normalized_status}
            if note:
                payload["note"] = str(note).strip()[:1000]
            cur.execute(
                """
                INSERT INTO reminder_events (
                    reminder_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (int(reminder_id), event_type, actor, _json_dumps(payload)),
            )
            conn.commit()
            updated = _fetch_reminder_instance(cur, int(reminder_id))

    return {"ok": True, "item": updated}


def update_reminder_fields(
    reminder_id: int,
    *,
    actor: str,
    assigned_to: Optional[str] = None,
    due_at: Optional[str] = None,
    title: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    with pg() as conn:
        with conn.cursor() as cur:
            current = _fetch_reminder_instance(cur, int(reminder_id))
            if not current:
                raise HTTPException(status_code=404, detail="Reminder not found")

            updates: List[str] = []
            params: List[Any] = []
            payload: Dict[str, Any] = {}

            if assigned_to is not None:
                assigned_to_value = str(assigned_to).strip() or None
                updates.append("assigned_to = %s")
                params.append(assigned_to_value)
                payload["assigned_to"] = assigned_to_value

            if due_at is not None:
                due_at_value = _parse_dt(due_at)
                updates.append("due_at = %s")
                params.append(due_at_value)
                payload["due_at"] = due_at_value

            if title is not None:
                title_value = str(title).strip()
                if not title_value:
                    raise HTTPException(status_code=400, detail="title cannot be empty")
                updates.append("title = %s")
                params.append(title_value[:250])
                payload["title"] = title_value[:250]

            if note is not None:
                note_value = str(note).strip() or None
                updates.append("summary = %s")
                params.append(note_value[:2000] if note_value else None)
                payload["summary"] = note_value[:2000] if note_value else None

            if not updates:
                return {"ok": True, "item": current}

            updates.append("updated_at = NOW()")
            params.append(int(reminder_id))
            cur.execute(
                f"""
                UPDATE reminder_instances
                SET {", ".join(updates)}
                WHERE id = %s
                """,
                params,
            )
            cur.execute(
                """
                INSERT INTO reminder_events (
                    reminder_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (int(reminder_id), "updated", actor, _json_dumps(payload)),
            )
            conn.commit()
            updated = _fetch_reminder_instance(cur, int(reminder_id))

    return {"ok": True, "item": updated}


def snooze_reminder(
    reminder_id: int,
    *,
    actor: str,
    snoozed_until: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    snooze_dt = _parse_dt(snoozed_until)
    if not snooze_dt:
        raise HTTPException(status_code=400, detail="snoozed_until must be a valid ISO datetime")

    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_reminder_instance(cur, int(reminder_id))
            if not item:
                raise HTTPException(status_code=404, detail="Reminder not found")

            cur.execute(
                """
                UPDATE reminder_instances
                SET
                    status = 'snoozed',
                    snoozed_until = %s,
                    completed_at = NULL,
                    canceled_at = NULL,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (snooze_dt, int(reminder_id)),
            )
            payload = {"snoozed_until": snooze_dt}
            if note:
                payload["note"] = str(note).strip()[:1000]
            cur.execute(
                """
                INSERT INTO reminder_events (
                    reminder_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (int(reminder_id), "snoozed", actor, _json_dumps(payload)),
            )
            conn.commit()
            updated = _fetch_reminder_instance(cur, int(reminder_id))

    return {"ok": True, "item": updated}


def list_alert_rule_configs() -> Dict[str, Any]:
    require_postgres_configured()
    with pg() as conn:
        with conn.cursor() as cur:
            payload: Dict[str, List[Dict[str, Any]]] = {}
            for table_name in ("alert_rule_config", "trend_rule_config", "revenue_rule_config"):
                cur.execute(f"SELECT * FROM {table_name} ORDER BY rule_code")
                payload[table_name] = [dict(row) for row in cur.fetchall()]
    return {"ok": True, "items": payload}


def list_technicians(
    *,
    search: Optional[str] = None,
    active_only: bool = False,
    with_current_assignments_only: bool = False,
    with_recent_route_activity_only: bool = False,
    field_only: bool = False,
    role_type: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> Dict[str, Any]:
    require_postgres_configured()
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, int(offset))
    params: List[Any] = []
    where_clauses: List[str] = []

    search_value = (search or "").strip()
    if search_value:
        where_clauses.append(
            """(
            tech_id ILIKE %s
            OR tech_name ILIKE %s
        )"""
        )
        pattern = f"%{search_value}%"
        params.extend([pattern, pattern])

    normalized_role_type = (role_type or "").strip()
    if normalized_role_type:
        where_clauses.append("COALESCE(role_type, '') = %s")
        params.append(normalized_role_type)

    if active_only:
        where_clauses.append("is_active = TRUE")

    if with_current_assignments_only:
        where_clauses.append("has_current_assignments = TRUE")

    if with_recent_route_activity_only:
        where_clauses.append("has_recent_route_activity = TRUE")

    if field_only:
        where_clauses.append("is_field_operator = TRUE")

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    base_cte = """
    WITH assignment_rollup AS (
        SELECT
            a.technician_id,
            COUNT(DISTINCT a.source_service_location_id) FILTER (
                WHERE a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
            ) AS service_location_count,
            COUNT(DISTINCT a.customer_id) FILTER (
                WHERE a.customer_id IS NOT NULL
                  AND a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
            ) AS customer_count,
            COUNT(DISTINCT a.customer_id) FILTER (
                WHERE a.customer_id IS NOT NULL
                  AND a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                  AND c.is_operationally_active = TRUE
            ) AS active_customer_count
        FROM service_location_technician_assignments a
        LEFT JOIN customers c ON c.id = a.customer_id
        GROUP BY a.technician_id
    ),
    route_stop_rollup AS (
        SELECT
            s.technician_id,
            MAX(s.service_date) FILTER (WHERE s.is_skipped = FALSE) AS latest_service_date,
            COUNT(*) FILTER (
                WHERE s.is_skipped = FALSE
                  AND s.service_date >= NOW() - INTERVAL '30 days'
            ) AS route_stop_count_30d
        FROM technician_route_stops s
        GROUP BY s.technician_id
    ),
    grouped AS (
        SELECT
            t.id AS technician_id,
            t.source_account_id AS tech_id,
            COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), NULLIF(t.username, ''), t.source_account_id) AS tech_name,
            t.first_name,
            t.last_name,
            t.username,
            t.role_type,
            t.is_active,
            COALESCE(ar.service_location_count, 0) AS service_location_count,
            COALESCE(ar.customer_count, 0) AS customer_count,
            COALESCE(ar.active_customer_count, 0) AS active_customer_count,
            rs.latest_service_date,
            COALESCE(rs.route_stop_count_30d, 0) AS route_stop_count_30d,
            (COALESCE(ar.service_location_count, 0) > 0) AS has_current_assignments,
            (COALESCE(rs.route_stop_count_30d, 0) > 0) AS has_recent_route_activity,
            (
                COALESCE(t.role_type, '') = 'Tech'
                OR COALESCE(ar.service_location_count, 0) > 0
                OR COALESCE(rs.route_stop_count_30d, 0) > 0
            ) AS is_field_operator
        FROM technicians t
        LEFT JOIN assignment_rollup ar ON ar.technician_id = t.id
        LEFT JOIN route_stop_rollup rs ON rs.technician_id = t.id
        WHERE t.source_system = 'skimmer'
          AND (
              COALESCE(t.role_type, '') = 'Tech'
              OR COALESCE(ar.service_location_count, 0) > 0
              OR COALESCE(rs.route_stop_count_30d, 0) > 0
          )
    )
    """

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                base_cte
                + f"""
                SELECT COUNT(*) AS total
                FROM grouped
                {where_sql}
                """,
                params,
            )
            total = int(cur.fetchone()["total"])

            cur.execute(
                base_cte
                + f"""
                SELECT
                    tech_id,
                    tech_name,
                    first_name,
                    last_name,
                    username,
                    role_type,
                    is_active,
                    service_location_count,
                    customer_count,
                    active_customer_count,
                    latest_service_date,
                    route_stop_count_30d,
                    has_current_assignments,
                    has_recent_route_activity,
                    is_field_operator
                FROM grouped
                {where_sql}
                ORDER BY tech_name ASC, tech_id ASC
                LIMIT %s OFFSET %s
                """,
                params + [safe_limit, safe_offset],
            )
            items = [dict(row) for row in cur.fetchall()]

            cur.execute(
                base_cte
                + """
                SELECT
                    COUNT(*) AS total_visible,
                    COUNT(*) FILTER (WHERE is_active = TRUE) AS active_count,
                    COUNT(*) FILTER (WHERE has_current_assignments = TRUE) AS current_assignment_count,
                    COUNT(*) FILTER (WHERE has_recent_route_activity = TRUE) AS recent_route_activity_count,
                    COUNT(*) FILTER (WHERE is_field_operator = TRUE) AS field_operator_count
                FROM grouped
                """,
            )
            summary = dict(cur.fetchone())

    return {
        "ok": True,
        "total": total,
        "limit": safe_limit,
        "offset": safe_offset,
        "items": items,
        "summary": summary,
        "filters": {
            "search": search_value or None,
            "active_only": bool(active_only),
            "with_current_assignments_only": bool(with_current_assignments_only),
            "with_recent_route_activity_only": bool(with_recent_route_activity_only),
            "field_only": bool(field_only),
            "role_type": normalized_role_type or None,
        },
        "source": "technicians + service_location_technician_assignments",
    }


def get_labor_payroll(
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    include_salary: bool = False,
) -> Dict[str, Any]:
    require_postgres_configured()

    parsed_start = _parse_date(start_date)
    parsed_end = _parse_date(end_date)
    default_start, default_end = _default_labor_week()
    range_start = parsed_start or default_start
    range_end = parsed_end or default_end
    if range_start > range_end:
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")

    api_counts = _skimmer_api_cleaning_counts(range_start, range_end)

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH tech_lookup AS (
                    SELECT
                        t.id AS technician_id,
                        t.source_account_id AS tech_id,
                        COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), NULLIF(t.username, ''), t.source_account_id) AS tech_name,
                        t.first_name,
                        t.last_name,
                        t.username,
                        t.role_type,
                        t.is_active
                    FROM technicians t
                    WHERE t.source_system = 'skimmer'
                      AND t.source_account_id IS NOT NULL
                ),
                filter_clean_rollup AS (
                    SELECT
                        tl.tech_id,
                        COUNT(DISTINCT w.id) AS filter_clean_count
                    FROM technician_route_stops s
                    JOIN tech_lookup tl ON tl.technician_id = s.technician_id
                    JOIN sk_work_order w
                      ON w.source_system = s.source_system
                     AND w.source_service_location_id = s.source_service_location_id
                     AND w.service_date::date = s.service_date::date
                    LEFT JOIN sk_work_order_type wt
                      ON wt.source_system = w.source_system
                     AND wt.source_work_order_type_id = w.source_work_order_type_id
                    WHERE s.technician_id IS NOT NULL
                      AND s.is_skipped = FALSE
                      AND s.service_date::date BETWEEN %s AND %s
                      AND COALESCE(w.is_deleted, FALSE) = FALSE
                      AND w.complete_time IS NOT NULL
                      AND w.complete_time >= TIMESTAMPTZ '2011-01-01 00:00:00+00'
                      AND (
                          lower(COALESCE(wt.description, '')) = 'filter clean'
                          OR lower(COALESCE(w.work_needed, '')) LIKE '%%filter clean%%'
                      )
                    GROUP BY tl.tech_id
                ),
                active_techs AS (
                    SELECT tech_id FROM tech_lookup
                    UNION
                    SELECT tech_id FROM filter_clean_rollup
                )
                SELECT
                    tl.tech_id,
                    tl.tech_name,
                    tl.first_name,
                    tl.last_name,
                    tl.username,
                    tl.role_type,
                    tl.is_active,
                    COALESCE(fc.filter_clean_count, 0) AS filter_clean_count
                FROM active_techs a
                JOIN tech_lookup tl ON tl.tech_id = a.tech_id
                LEFT JOIN filter_clean_rollup fc ON fc.tech_id = a.tech_id
                ORDER BY tl.tech_name ASC, tl.tech_id ASC
                """,
                [range_start, range_end],
            )
            rows = [dict(row) for row in cur.fetchall()]

    items: List[Dict[str, Any]] = []
    summary = {
        "tech_count": 0,
        "payable_tech_count": 0,
        "salary_tech_count": 0,
        "total_pools": 0,
        "total_regular_pools": 0,
        "total_commission_pools": 0,
        "total_filter_cleans": 0,
        "total_regular_pay": 0.0,
        "total_commission_pay": 0.0,
        "total_filter_clean_pay": 0.0,
        "total_pay": 0.0,
    }

    row_map = {str(row.get("tech_id") or ""): row for row in rows}
    all_tech_ids = sorted(set(row_map.keys()) | set(api_counts.keys()))

    for tech_id in all_tech_ids:
        row = row_map.get(tech_id, {"tech_id": tech_id, "tech_name": api_counts.get(tech_id, {}).get("tech_name") or tech_id})
        api_row = api_counts.get(tech_id, {})
        is_salary = _is_salary_tech(row)
        pool_count = int(api_row.get("pool_count") or 0)
        filter_clean_count = int(row.get("filter_clean_count") or 0)
        if pool_count <= 0 and filter_clean_count <= 0:
            continue
        if is_salary and not include_salary:
            continue
        regular_pool_count = min(pool_count, LABOR_REGULAR_POOL_CAP)
        commission_pool_count = max(0, pool_count - LABOR_REGULAR_POOL_CAP)

        regular_pool_pay = 0.0 if is_salary else regular_pool_count * LABOR_POOL_RATE
        commission_pool_pay = 0.0 if is_salary else commission_pool_count * LABOR_POOL_RATE
        filter_clean_pay = 0.0 if is_salary else filter_clean_count * LABOR_FILTER_CLEAN_RATE
        gusto_commission_amount = commission_pool_pay + filter_clean_pay
        total_pay = regular_pool_pay + commission_pool_pay + filter_clean_pay

        item = {
            "tech_id": row.get("tech_id"),
            "tech_name": row.get("tech_name"),
            "first_name": row.get("first_name"),
            "last_name": row.get("last_name"),
            "username": row.get("username"),
            "role_type": row.get("role_type"),
            "is_active": bool(row.get("is_active")),
            "is_salary": is_salary,
            "pool_count": pool_count,
            "service_day_count": len(api_row.get("completed_service_days") or []),
            "regular_pool_count": regular_pool_count,
            "commission_pool_count": commission_pool_count,
            "filter_clean_count": filter_clean_count,
            "regular_pool_pay": regular_pool_pay,
            "commission_pool_pay": commission_pool_pay,
            "filter_clean_pay": filter_clean_pay,
            "total_pay": total_pay,
            "gusto_regular_hours": 0 if is_salary else regular_pool_count,
            "gusto_commission_amount": 0.0 if is_salary else gusto_commission_amount,
            "notes": "Salary tech" if is_salary else "",
        }
        items.append(item)

        summary["tech_count"] += 1
        summary["salary_tech_count"] += 1 if is_salary else 0
        summary["payable_tech_count"] += 0 if is_salary else 1
        summary["total_pools"] += pool_count
        summary["total_regular_pools"] += regular_pool_count
        summary["total_commission_pools"] += commission_pool_count
        summary["total_filter_cleans"] += filter_clean_count
        summary["total_regular_pay"] += regular_pool_pay
        summary["total_commission_pay"] += commission_pool_pay
        summary["total_filter_clean_pay"] += filter_clean_pay
        summary["total_pay"] += total_pay

    items.sort(key=lambda item: (item["is_salary"], -(item["total_pay"]), str(item["tech_name"] or "")))

    return {
        "ok": True,
        "range": {
            "start_date": range_start.isoformat(),
            "end_date": range_end.isoformat(),
            "label": f"{range_start.isoformat()} to {range_end.isoformat()}",
            "timezone": DASHBOARD_TIMEZONE,
        },
        "pay_rules": {
            "pool_rate": LABOR_POOL_RATE,
            "filter_clean_rate": LABOR_FILTER_CLEAN_RATE,
            "regular_pool_cap": LABOR_REGULAR_POOL_CAP,
            "salary_tech_names": list(LABOR_SALARY_TECH_NAMES),
            "gusto_regular_unit_label": "hours",
            "gusto_commission_label": "commission",
        },
        "summary": summary,
        "items": items,
        "filters": {
            "include_salary": bool(include_salary),
        },
        "source": "skimmer route api + sk_work_order",
    }


def get_technician_detail(tech_id: str) -> Dict[str, Any]:
    require_postgres_configured()
    tech_id_value = str(tech_id).strip()
    if not tech_id_value:
        raise HTTPException(status_code=400, detail="tech_id is required")

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id AS technician_id,
                    t.source_account_id AS tech_id,
                    COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), NULLIF(t.username, ''), t.source_account_id) AS tech_name,
                    t.first_name,
                    t.last_name,
                    t.username,
                    t.email,
                    t.mobile_phone,
                    t.role_type,
                    t.is_active,
                    COALESCE(assignment_rollup.service_location_count, 0) AS service_location_count,
                    COALESCE(assignment_rollup.customer_count, 0) AS customer_count,
                    COALESCE(assignment_rollup.active_customer_count, 0) AS active_customer_count,
                    route_stop_rollup.latest_service_date,
                    COALESCE(route_stop_rollup.route_stop_count_30d, 0) AS route_stop_count_30d,
                    (COALESCE(assignment_rollup.service_location_count, 0) > 0) AS has_current_assignments,
                    (COALESCE(route_stop_rollup.route_stop_count_30d, 0) > 0) AS has_recent_route_activity,
                    (
                        COALESCE(t.role_type, '') = 'Tech'
                        OR COALESCE(assignment_rollup.service_location_count, 0) > 0
                        OR COALESCE(route_stop_rollup.route_stop_count_30d, 0) > 0
                    ) AS is_field_operator
                FROM technicians t
                LEFT JOIN (
                    SELECT
                        a.technician_id,
                        COUNT(DISTINCT a.source_service_location_id) FILTER (
                            WHERE a.is_deleted = FALSE
                              AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                        ) AS service_location_count,
                        COUNT(DISTINCT a.customer_id) FILTER (
                            WHERE a.customer_id IS NOT NULL
                              AND a.is_deleted = FALSE
                              AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                        ) AS customer_count,
                        COUNT(DISTINCT a.customer_id) FILTER (
                            WHERE a.customer_id IS NOT NULL
                              AND a.is_deleted = FALSE
                              AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                              AND c.is_operationally_active = TRUE
                        ) AS active_customer_count
                    FROM service_location_technician_assignments a
                    LEFT JOIN customers c ON c.id = a.customer_id
                    GROUP BY a.technician_id
                ) AS assignment_rollup ON assignment_rollup.technician_id = t.id
                LEFT JOIN (
                    SELECT
                        s.technician_id,
                        MAX(s.service_date) FILTER (WHERE s.is_skipped = FALSE) AS latest_service_date,
                        COUNT(*) FILTER (
                            WHERE s.is_skipped = FALSE
                              AND s.service_date >= NOW() - INTERVAL '30 days'
                        ) AS route_stop_count_30d
                    FROM technician_route_stops s
                    GROUP BY s.technician_id
                ) AS route_stop_rollup ON route_stop_rollup.technician_id = t.id
                WHERE t.source_system = 'skimmer'
                  AND t.source_account_id = %s
                """,
                (tech_id_value,),
            )
            item = cur.fetchone()
            if not item:
                raise HTTPException(status_code=404, detail="Technician not found")

            cur.execute(
                """
                SELECT DISTINCT
                    a.customer_id,
                    a.source_customer_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                        NULLIF(c.company_name, ''),
                        a.source_customer_id
                    ) AS customer_name,
                    c.customer_status,
                    c.is_operationally_active
                FROM service_location_technician_assignments a
                LEFT JOIN customers c ON c.id = a.customer_id
                JOIN technicians t ON t.id = a.technician_id
                WHERE t.source_system = 'skimmer'
                  AND t.source_account_id = %s
                  AND a.customer_id IS NOT NULL
                  AND a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                ORDER BY customer_name ASC
                LIMIT 200
                """,
                (tech_id_value,),
            )
            customers = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT DISTINCT
                    a.customer_id,
                    a.sk_service_location_id AS service_location_id,
                    a.source_service_location_id AS source_location_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                        NULLIF(c.company_name, ''),
                        a.source_customer_id
                    ) AS customer_name,
                    c.customer_status,
                    c.is_operationally_active,
                    sl.address,
                    sl.city,
                    sl.state,
                    sl.zip,
                    a.source_customer_id,
                    a.day_of_week,
                    a.frequency,
                    a.start_date,
                    a.end_date,
                    a.sequence,
                    a.status,
                    CASE COALESCE(a.day_of_week, '')
                        WHEN 'Monday' THEN 1
                        WHEN 'Tuesday' THEN 2
                        WHEN 'Wednesday' THEN 3
                        WHEN 'Thursday' THEN 4
                        WHEN 'Friday' THEN 5
                        WHEN 'Saturday' THEN 6
                        WHEN 'Sunday' THEN 7
                        ELSE 8
                    END AS weekday_sort
                FROM service_location_technician_assignments a
                LEFT JOIN customers c ON c.id = a.customer_id
                LEFT JOIN sk_service_location sl
                  ON sl.source_system = a.source_system
                 AND sl.source_location_id = a.source_service_location_id
                JOIN technicians t ON t.id = a.technician_id
                WHERE t.source_system = 'skimmer'
                  AND t.source_account_id = %s
                  AND a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                ORDER BY
                    weekday_sort,
                    a.sequence ASC NULLS LAST,
                    customer_name ASC,
                    sl.city ASC,
                    sl.address ASC,
                    a.sk_service_location_id ASC
                LIMIT 200
                """,
                (tech_id_value,),
            )
            service_locations = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                SELECT
                    a.source_service_location_id,
                    p.id AS pool_id,
                    COALESCE(NULLIF(trim(p.name), ''), CONCAT('Pool ', p.id)) AS pool_name,
                    COALESCE(
                        SUM(d.estimated_cost) FILTER (
                            WHERE d.service_date >= date_trunc('month', CURRENT_DATE)
                        ),
                        0
                    ) AS spend_month_to_date,
                    COALESCE(
                        SUM(d.estimated_cost) FILTER (
                            WHERE d.service_date >= NOW() - INTERVAL '30 days'
                        ),
                        0
                    ) AS spend_30d
                FROM service_location_technician_assignments a
                JOIN technicians t ON t.id = a.technician_id
                JOIN pools p
                  ON p.source_system = a.source_system
                 AND p.source_service_location_id = a.source_service_location_id
                LEFT JOIN chemical_dose_events d
                  ON d.pool_id = p.id
                 AND d.source_system = p.source_system
                WHERE t.source_system = 'skimmer'
                  AND t.source_account_id = %s
                  AND a.is_deleted = FALSE
                  AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                GROUP BY a.source_service_location_id, p.id, p.name
                ORDER BY a.source_service_location_id ASC, pool_name ASC
                """,
                (tech_id_value,),
            )
            pools_by_location: Dict[str, List[Dict[str, Any]]] = {}
            for row in cur.fetchall():
                pool_item = dict(row)
                key = str(pool_item.get("source_service_location_id") or "")
                pools_by_location.setdefault(key, []).append(pool_item)

            for location in service_locations:
                key = str(location.get("source_location_id") or "")
                location["pools"] = pools_by_location.get(key, [])

            cur.execute(
                """
                SELECT
                    s.service_date,
                    s.is_skipped,
                    s.sequence,
                    s.minutes_at_stop,
                    s.source_service_location_id,
                    COALESCE(
                        NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''),
                        NULLIF(c.company_name, ''),
                        s.source_customer_id
                    ) AS customer_name,
                    sl.address,
                    sl.city,
                    sl.state,
                    sl.zip
                FROM technician_route_stops s
                JOIN technicians t ON t.id = s.technician_id
                LEFT JOIN customers c ON c.id = s.customer_id
                LEFT JOIN sk_service_location sl
                  ON sl.source_system = s.source_system
                 AND sl.source_location_id = s.source_service_location_id
                WHERE t.source_system = 'skimmer'
                  AND t.source_account_id = %s
                ORDER BY s.service_date DESC, s.sequence ASC NULLS LAST
                LIMIT 50
                """,
                (tech_id_value,),
            )
            recent_route_stops = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                WITH tech_dose_events AS (
                    SELECT DISTINCT
                        d.id,
                        d.service_date,
                        d.pool_id,
                        d.estimated_cost
                    FROM technician_route_stops s
                    JOIN technicians t ON t.id = s.technician_id
                    JOIN pools p
                      ON p.source_system = s.source_system
                     AND p.source_service_location_id = s.source_service_location_id
                    JOIN chemical_dose_events d
                      ON d.pool_id = p.id
                     AND d.source_system = s.source_system
                     AND d.service_date::date = s.service_date::date
                    WHERE t.source_system = 'skimmer'
                      AND t.source_account_id = %s
                      AND s.is_skipped = FALSE
                )
                SELECT
                    COALESCE(
                        SUM(estimated_cost) FILTER (
                            WHERE service_date >= date_trunc('month', CURRENT_DATE)
                        ),
                        0
                    ) AS cost_month_to_date,
                    COALESCE(
                        SUM(estimated_cost) FILTER (
                            WHERE service_date >= NOW() - INTERVAL '30 days'
                        ),
                        0
                    ) AS cost_30d,
                    COALESCE(SUM(estimated_cost), 0) AS lifetime_cost,
                    COALESCE(
                        SUM(estimated_cost) FILTER (
                            WHERE service_date >= NOW() - INTERVAL '30 days'
                        ) / NULLIF(COUNT(DISTINCT pool_id) FILTER (
                            WHERE service_date >= NOW() - INTERVAL '30 days'
                        ), 0),
                        0
                    ) AS avg_spend_per_pool_30d
                FROM tech_dose_events
                """,
                (tech_id_value,),
            )
            chemical_spend_summary = dict(cur.fetchone() or {})

            cur.execute(
                """
                WITH recent_stops AS (
                    SELECT
                        s.service_date,
                        s.start_time,
                        s.minutes_at_stop,
                        s.source_service_location_id
                    FROM technician_route_stops s
                    JOIN technicians t ON t.id = s.technician_id
                    WHERE t.source_system = 'skimmer'
                      AND t.source_account_id = %s
                      AND s.is_skipped = FALSE
                      AND s.service_date >= NOW() - INTERVAL '30 days'
                ),
                route_days AS (
                    SELECT
                        service_date::date AS route_day,
                        MIN(start_time AT TIME ZONE %s) AS first_start_local
                    FROM recent_stops
                    WHERE start_time IS NOT NULL
                    GROUP BY service_date::date
                )
                SELECT
                    COUNT(*) AS stop_count_30d,
                    COALESCE(
                        AVG(minutes_at_stop) FILTER (WHERE minutes_at_stop IS NOT NULL),
                        0
                    ) AS avg_minutes_per_pool_30d,
                    COUNT(*) FILTER (WHERE minutes_at_stop > 45) AS long_stop_count_30d,
                    COUNT(*) FILTER (WHERE minutes_at_stop < 10) AS short_stop_count_30d,
                    COUNT(*) FILTER (WHERE minutes_at_stop IS NOT NULL) AS timed_stop_count_30d,
                    COALESCE(
                        (SUM(minutes_at_stop) FILTER (WHERE minutes_at_stop IS NOT NULL))::NUMERIC
                        / NULLIF(COUNT(DISTINCT source_service_location_id) FILTER (WHERE minutes_at_stop IS NOT NULL), 0),
                        0
                    ) AS avg_minutes_per_assigned_pool_30d,
                    (SELECT COUNT(*) FROM route_days) AS route_day_count_30d,
                    (SELECT COUNT(*) FROM route_days WHERE first_start_local::time > TIME '10:00') AS late_start_count_30d,
                    (SELECT MIN(first_start_local::time) FROM route_days) AS earliest_route_start_30d,
                    (SELECT MAX(first_start_local::time) FROM route_days) AS latest_route_start_30d
                FROM recent_stops
                """,
                (tech_id_value, DASHBOARD_TIMEZONE),
            )
            route_timing_summary = dict(cur.fetchone() or {})

            cur.execute(
                """
                WITH assigned_pools AS (
                    SELECT DISTINCT p.id AS pool_id
                    FROM service_location_technician_assignments a
                    JOIN technicians t ON t.id = a.technician_id
                    JOIN pools p
                      ON p.source_system = a.source_system
                     AND p.source_service_location_id = a.source_service_location_id
                    WHERE t.source_system = 'skimmer'
                      AND t.source_account_id = %s
                      AND a.is_deleted = FALSE
                      AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                )
                SELECT
                    ai.id,
                    ai.category,
                    ai.rule_code,
                    ai.status,
                    ai.severity,
                    ai.title,
                    ai.summary,
                    ai.last_detected_at,
                    ai.pool_id,
                    ai.customer_id,
                    p.name AS pool_name,
                    COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), NULLIF(c.company_name, ''), NULL) AS customer_name
                FROM alert_instances ai
                JOIN assigned_pools ap ON ap.pool_id = ai.pool_id
                LEFT JOIN pools p ON p.id = ai.pool_id
                LEFT JOIN customers c ON c.id = ai.customer_id
                WHERE ai.status IN ('open', 'acknowledged', 'snoozed')
                ORDER BY ai.last_detected_at DESC NULLS LAST, ai.id DESC
                LIMIT 50
                """,
                (tech_id_value,),
            )
            associated_alerts = [dict(row) for row in cur.fetchall()]

            cur.execute(
                """
                WITH assigned_pools AS (
                    SELECT DISTINCT p.id AS pool_id
                    FROM service_location_technician_assignments a
                    JOIN technicians t ON t.id = a.technician_id
                    JOIN pools p
                      ON p.source_system = a.source_system
                     AND p.source_service_location_id = a.source_service_location_id
                    WHERE t.source_system = 'skimmer'
                      AND t.source_account_id = %s
                      AND a.is_deleted = FALSE
                      AND (a.end_date IS NULL OR a.end_date >= CURRENT_DATE)
                )
                SELECT
                    r.id,
                    r.status,
                    r.priority,
                    r.title,
                    r.summary,
                    r.due_at,
                    r.pool_id,
                    r.customer_id,
                    p.name AS pool_name,
                    COALESCE(NULLIF(trim(concat_ws(' ', c.first_name, c.last_name)), ''), NULLIF(c.company_name, ''), NULL) AS customer_name
                FROM reminder_instances r
                JOIN assigned_pools ap ON ap.pool_id = r.pool_id
                LEFT JOIN pools p ON p.id = r.pool_id
                LEFT JOIN customers c ON c.id = r.customer_id
                WHERE r.status IN ('open', 'acknowledged', 'snoozed')
                ORDER BY r.due_at ASC NULLS LAST, r.id DESC
                LIMIT 50
                """,
                (tech_id_value,),
            )
            associated_reminders = [dict(row) for row in cur.fetchall()]

    return {
        "ok": True,
        "item": dict(item),
        "customers": customers,
        "service_locations": service_locations,
        "recent_route_stops": recent_route_stops,
        "chemical_spend_summary": chemical_spend_summary,
        "route_timing_summary": route_timing_summary,
        "associated_alerts": associated_alerts,
        "associated_reminders": associated_reminders,
        "source": "technicians + service_location_technician_assignments",
    }


def update_alert_instance_status(
    alert_id: int,
    *,
    next_status: str,
    actor: str,
    note: Optional[str] = None,
    snoozed_until: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    normalized_status = _normalize_status(next_status)
    if normalized_status not in ("acknowledged", "resolved", "snoozed"):
        raise HTTPException(status_code=400, detail=f"Unsupported next status '{next_status}'")

    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_alert_instance(cur, int(alert_id))
            if not item:
                raise HTTPException(status_code=404, detail="Alert not found")

            current_status = _normalize_status(item.get("status"))
            if normalized_status == "acknowledged" and current_status == "resolved":
                return {"ok": True, "item": item}

            snooze_dt = None
            if normalized_status == "snoozed":
                snooze_dt = _parse_dt(snoozed_until)
                if not snooze_dt:
                    raise HTTPException(status_code=400, detail="snoozed_until must be a valid ISO datetime")

            now_field_updates = []
            params: List[Any] = []

            if normalized_status == "acknowledged":
                now_field_updates.append("status = %s")
                params.append("acknowledged")
                now_field_updates.append("acknowledged_at = COALESCE(acknowledged_at, NOW())")
                now_field_updates.append("snoozed_until = NULL")
                now_field_updates.append("updated_at = NOW()")
                event_type = "acknowledged"
            elif normalized_status == "snoozed":
                now_field_updates.append("status = %s")
                params.append("snoozed")
                now_field_updates.append("snoozed_until = %s")
                params.append(snooze_dt)
                now_field_updates.append("acknowledged_at = COALESCE(acknowledged_at, NOW())")
                now_field_updates.append("updated_at = NOW()")
                event_type = "snoozed"
            else:
                now_field_updates.append("status = %s")
                params.append("resolved")
                now_field_updates.append("resolved_at = NOW()")
                now_field_updates.append("snoozed_until = NULL")
                now_field_updates.append("updated_at = NOW()")
                event_type = "resolved"

            params.append(int(alert_id))
            cur.execute(
                f"""
                UPDATE alert_instances
                SET {", ".join(now_field_updates)}
                WHERE id = %s
                """,
                params,
            )

            payload = {"status": normalized_status}
            if snooze_dt is not None:
                payload["snoozed_until"] = snooze_dt
            if note:
                payload["note"] = str(note).strip()[:1000]
            cur.execute(
                """
                INSERT INTO alert_instance_events (
                    alert_instance_id,
                    event_type,
                    actor,
                    payload_json
                ) VALUES (%s, %s, %s, %s::jsonb)
                """,
                (int(alert_id), event_type, actor, _json_dumps(payload)),
            )
            conn.commit()

            updated = _fetch_alert_instance(cur, int(alert_id))
            if not updated:
                raise HTTPException(status_code=404, detail="Alert not found after update")

    return {"ok": True, "item": updated}
