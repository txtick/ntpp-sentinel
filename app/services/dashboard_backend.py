from typing import Any, Dict, Optional

from fastapi import HTTPException

from pg import DATABASE_URL, ensure_pg_schema, pg, pg_healthcheck


def ensure_web_backend_schema() -> None:
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
            cur.execute(
                """
                SELECT to_regclass('public.dashboard_summary_v') AS exists_name
                """
            )
            row = cur.fetchone()
            if not row or not row.get("exists_name"):
                return None

            cur.execute("SELECT * FROM dashboard_summary_v")
            summary = cur.fetchone()
            return dict(summary) if summary else None
