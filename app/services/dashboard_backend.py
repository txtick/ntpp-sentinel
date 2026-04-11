import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from pg import DATABASE_URL, ensure_pg_schema, pg, pg_healthcheck
from services.dashboard_schema import ensure_dashboard_schema_definitions

MANAGED_ALERT_CATEGORIES = ("pool", "process", "revenue")
ACTIONABLE_ALERT_STATUSES = ("open", "acknowledged", "snoozed", "resolved", "cleared")
MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD = float(
    os.getenv("MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD", "75")
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


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


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
    reading_key = row.get("reading_key") or row.get("opportunity_type") or "alert"
    if reading_key == "fc_cya_ratio":
        reading_key = "FC:CYA ratio"
    if category == "revenue":
        return f"{customer_name}: {row.get('opportunity_type') or 'revenue opportunity'}"
    return f"{customer_name}: {reading_key} on {pool_name}"


def _alert_summary(category: str, row: Dict[str, Any]) -> str:
    if category == "revenue":
        opportunity_type = row.get("opportunity_type") or "opportunity"
        observed = row.get("observed_count")
        return f"{opportunity_type} detected" if observed is None else f"{opportunity_type} detected from observed value {observed}"

    observed_value = row.get("value")
    if observed_value is None:
        observed_value = row.get("observed_value")
    threshold_value = row.get("threshold_value")
    reading_key = row.get("reading_key") or "metric"
    if reading_key == "fc_cya_ratio" and observed_value is not None and threshold_value is not None:
        try:
            observed_pct = float(observed_value) * 100.0
            threshold_pct = float(threshold_value) * 100.0
            return f"FC:CYA ratio was {observed_pct:.1f}% vs minimum {threshold_pct:.1f}% on the last 2 paired readings"
        except Exception:
            pass
    if threshold_value is None:
        return f"{reading_key} alert detected"
    return f"{reading_key} observed {observed_value} against threshold {threshold_value}"


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
                NULL::NUMERIC AS threshold_value,
                NULL::NUMERIC AS value,
                NULL::NUMERIC AS observed_value,
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

    return {"ok": True, "item": item, "events": events}


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
                        MIN(start_time) AS first_start_time
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
                    (SELECT COUNT(*) FROM route_days WHERE first_start_time::time > TIME '10:00') AS late_start_count_30d,
                    (SELECT MIN(first_start_time) FROM route_days) AS earliest_route_start_30d,
                    (SELECT MAX(first_start_time) FROM route_days) AS latest_route_start_30d
                FROM recent_stops
                """,
                (tech_id_value,),
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
