import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from pg import DATABASE_URL, ensure_pg_schema, pg, pg_healthcheck

MANAGED_ALERT_CATEGORIES = ("pool", "process", "revenue")
ACTIONABLE_ALERT_STATUSES = ("open", "acknowledged", "snoozed", "resolved", "cleared")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=str)


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
    previous = os.environ.get("INGEST_OWNS_DASHBOARD_SCHEMA")
    os.environ["INGEST_OWNS_DASHBOARD_SCHEMA"] = "1"
    try:
        from ingest.pipeline import ensure_operational_schema

        ensure_operational_schema()
    finally:
        if previous is None:
            os.environ.pop("INGEST_OWNS_DASHBOARD_SCHEMA", None)
        else:
            os.environ["INGEST_OWNS_DASHBOARD_SCHEMA"] = previous


def _alert_title(category: str, row: Dict[str, Any]) -> str:
    customer_name = row.get("customer_name") or "Unknown Customer"
    pool_name = row.get("pool_name") or "No Pool"
    reading_key = row.get("reading_key") or row.get("opportunity_type") or "alert"
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
            item["metadata_json"] = _build_metadata("pool", item, refresh_run_id)
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
            item["metadata_json"] = _build_metadata("process", item, refresh_run_id)
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
            item["metadata_json"] = _build_metadata("revenue", item, refresh_run_id)
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
            return dict(summary) if summary else None


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

    if normalized_status:
        if normalized_status not in ACTIONABLE_ALERT_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unsupported status '{normalized_status}'")
        filters.append("status = %s")
        params.append(normalized_status)
    if normalized_category:
        filters.append("category = %s")
        params.append(normalized_category)

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


def update_alert_instance_status(
    alert_id: int,
    *,
    next_status: str,
    actor: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    require_postgres_configured()
    normalized_status = _normalize_status(next_status)
    if normalized_status not in ("acknowledged", "resolved"):
        raise HTTPException(status_code=400, detail=f"Unsupported next status '{next_status}'")

    with pg() as conn:
        with conn.cursor() as cur:
            item = _fetch_alert_instance(cur, int(alert_id))
            if not item:
                raise HTTPException(status_code=404, detail="Alert not found")

            current_status = _normalize_status(item.get("status"))
            if normalized_status == "acknowledged" and current_status == "resolved":
                return {"ok": True, "item": item}

            now_field_updates = []
            params: List[Any] = []

            if normalized_status == "acknowledged":
                now_field_updates.append("status = %s")
                params.append("acknowledged")
                now_field_updates.append("acknowledged_at = COALESCE(acknowledged_at, NOW())")
                now_field_updates.append("snoozed_until = NULL")
                now_field_updates.append("updated_at = NOW()")
                event_type = "acknowledged"
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
