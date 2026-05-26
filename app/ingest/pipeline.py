import hashlib
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from pg import ensure_pg_schema, pg
from services.dashboard_schema import ensure_dashboard_schema_definitions
from scripts.import_skimmer_customers import import_skimmer_data

TIMEZONE_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))

REQUIRED_TABLE_COLUMNS: Dict[str, List[str]] = {
    "Customer": [
        "id",
        "FirstName",
        "LastName",
        "CompanyName",
        "PrimaryEmail",
        "MobilePhone",
        "MobilePhone2",
        "BillingAddress",
        "BillingCity",
        "BillingState",
        "BillingZip",
        "IsInactive",
        "IsLead",
    ],
    "Pool": [
        "id",
        "ServiceLocationId",
        "Name",
        "Gallons",
        "BaselineFilterPressure",
        "Notes",
        "EquipmentItems",
        "CompanyId",
    ],
    "ServiceLocation": [
        "id",
        "CustomerId",
        "Address",
        "City",
        "State",
        "Zip",
        "LocationCode",
        "Latitude",
        "Longitude",
        "MinutesAtStop",
        "IsBadAddress",
        "GateCode",
        "DogsName",
        "Notes",
        "Rate",
        "RateType",
        "LaborCost",
        "LaborCostType",
        "Pools",
        "RouteAssignments",
        "RouteMoves",
        "WorkOrderModels",
        "RecurringWorkItems",
        "CompanyId",
    ],
    "Account": [
        "id",
        "Username",
        "Email",
        "FirstName",
        "LastName",
        "RoleType",
        "IsActive",
        "MobilePhone",
        "Address",
        "City",
        "State",
        "Zip",
        "CompanyId",
    ],
    "RouteAssignment": [
        "id",
        "ServiceLocationId",
        "AccountId",
        "DayOfWeek",
        "Frequency",
        "StartDate",
        "EndDate",
        "Sequence",
        "CompanyId",
    ],
    "RouteStop": [
        "id",
        "AccountId",
        "ServiceLocationId",
        "RouteAssignmentId",
        "ServiceDate",
        "StartTime",
        "CompleteTime",
        "IsSkipped",
        "Sequence",
        "MinutesAtStop",
        "CompanyId",
    ],
    "EntryDescription": [
        "id",
        "Description",
        "UnitOfMeasure",
        "EntryType",
        "ReadingType",
        "DosageType",
        "SelectedIndex",
        "Sequence",
        "Cost",
        "Price",
        "CanIncludeWithService",
        "ColumnSequence",
        "CompanyId",
    ],
    "ServiceStopEntry": [
        "id",
        "ServiceStopId",
        "PoolId",
        "EntryDescriptionId",
        "EntryType",
        "Value",
        "ServiceDate",
        "CompanyId",
        "EntryDescriptionText",
        "UnitOfMeasure",
        "ReadingType",
        "SelectedIndex",
        "Sequence",
        "ValueDisplay",
        "WorkOrderId",
    ],
    "WorkOrderType": ["id", "Description", "DefaultWorkNeeded", "DefaultEmailHeader", "DefaultEmailMessage"],
    "Quote": ["id", "CustomerId", "Status", "Total", "QuoteDate"],
    "QuoteLocation": ["id", "QuoteId"],
    "QuoteItem": ["id", "QuoteLocationId", "Item", "Quantity", "Rate"],
    "WorkOrder": ["id", "WorkOrderTypeId", "ServiceLocationId", "WorkNeeded", "ServiceDate", "CompleteTime", "Price"],
}

IMPORT_TABLES = [
    "Customer",
    "ServiceLocation",
    "Pool",
    "Account",
    "RouteAssignment",
    "RouteStop",
    "WorkOrderType",
    "WorkOrder",
    "EntryDescription",
    "ServiceStopEntry",
    "Quote",
    "QuoteLocation",
    "QuoteItem",
]

CRITICAL_COUNT_KEY_MAP = {
    "Customer": "customers_imported",
    "Pool": "pools_imported",
    "ServiceLocation": "service_locations_imported",
    "EntryDescription": "entry_descriptions_imported",
    "ServiceStopEntry": "service_stop_entries_imported",
}

FATAL_DROP_RATIO = float(os.getenv("INGEST_FATAL_DROP_RATIO", "0.50"))
INACTIVE_PRUNE_DAYS = int(os.getenv("INACTIVE_PRUNE_DAYS", "60"))
PIPELINE_LOCK_KEY = int(os.getenv("INGEST_PIPELINE_LOCK_KEY", "241103"))
MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD = float(
    os.getenv("MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD", "75")
)
SKIP_DUPLICATE_SOURCE_SUCCESS = os.getenv(
    "INGEST_SKIP_DUPLICATE_SOURCE_SUCCESS", "1"
).lower() in ("1", "true", "yes", "on")
INGEST_OWNS_DASHBOARD_SCHEMA = os.getenv("INGEST_OWNS_DASHBOARD_SCHEMA", "0").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
WEB_BACKEND_BASE_URL = os.getenv("WEB_BACKEND_BASE_URL", "http://web-backend:8020").rstrip("/")
WEB_BACKEND_REFRESH_ENABLED = os.getenv("WEB_BACKEND_REFRESH_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
WEB_BACKEND_REFRESH_TIMEOUT_SECONDS = float(os.getenv("WEB_BACKEND_REFRESH_TIMEOUT_SECONDS", "30"))
WEB_BACKEND_SECRET = os.getenv("WEB_BACKEND_SECRET", "").strip() or os.getenv("WEBHOOK_SECRET", "").strip()
SENTINEL_BASE_URL = os.getenv("SENTINEL_BASE_URL", "http://sentinel:8000").rstrip("/")
CUSTOMER_SYNC_AFTER_INGEST_ENABLED = os.getenv("CUSTOMER_SYNC_AFTER_INGEST_ENABLED", "1").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
CUSTOMER_SYNC_AFTER_INGEST_LIMIT = int(os.getenv("CUSTOMER_SYNC_AFTER_INGEST_LIMIT", "1000"))
CUSTOMER_SYNC_AFTER_INGEST_TIMEOUT_SECONDS = float(
    os.getenv("CUSTOMER_SYNC_AFTER_INGEST_TIMEOUT_SECONDS", "300")
)
CUSTOMER_SYNC_SECRET = os.getenv("CUSTOMER_SYNC_SECRET", "").strip() or os.getenv("WEBHOOK_SECRET", "").strip()


class PipelineValidationError(RuntimeError):
    pass


class PipelineBusyError(RuntimeError):
    pass


def _log(msg: str) -> None:
    stamp = datetime.now(ZoneInfo(TIMEZONE_NAME)).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] [ingest-worker] {msg}")


def _trigger_web_backend_refresh(
    *,
    pipeline_run_id: int,
    source_system: str,
    trigger_reason: str,
) -> Dict[str, Any]:
    if not WEB_BACKEND_REFRESH_ENABLED:
        return {"status": "skipped", "reason": "WEB_BACKEND_REFRESH_ENABLED is false"}
    if not WEB_BACKEND_BASE_URL:
        return {"status": "skipped", "reason": "WEB_BACKEND_BASE_URL is not configured"}

    payload = json.dumps(
        {
            "trigger_reason": f"ingest_pipeline:{trigger_reason}",
            "pipeline_run_id": pipeline_run_id,
            "source_system": source_system,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{WEB_BACKEND_BASE_URL}/jobs/dashboard/refresh?trigger_reason=ingest_pipeline",
        data=payload,
        headers={
            "Content-Type": "application/json",
            **({"X-NTPP-Secret": WEB_BACKEND_SECRET} if WEB_BACKEND_SECRET else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=WEB_BACKEND_REFRESH_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8").strip()
            if not body:
                return {"status": "ok"}
            try:
                parsed = json.loads(body)
                return parsed if isinstance(parsed, dict) else {"status": "ok", "body": body}
            except Exception:
                return {"status": "ok", "body": body}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"status": "error", "code": exc.code, "detail": detail[:1000]}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:1000]}


def _trigger_customer_sync_after_ingest(
    *,
    pipeline_run_id: int,
    source_system: str,
    trigger_reason: str,
) -> Dict[str, Any]:
    if not CUSTOMER_SYNC_AFTER_INGEST_ENABLED:
        return {"status": "skipped", "reason": "CUSTOMER_SYNC_AFTER_INGEST_ENABLED is false"}
    if not SENTINEL_BASE_URL:
        return {"status": "skipped", "reason": "SENTINEL_BASE_URL is not configured"}

    payload = json.dumps(
        {
            "trigger_reason": f"ingest_pipeline:{trigger_reason}",
            "pipeline_run_id": pipeline_run_id,
            "source_system": source_system,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        (
            f"{SENTINEL_BASE_URL}/jobs/skimmer_customer_sync"
            f"?dry_run=0&limit={max(1, CUSTOMER_SYNC_AFTER_INGEST_LIMIT)}"
        ),
        data=payload,
        headers={
            "Content-Type": "application/json",
            **({"X-NTPP-Secret": CUSTOMER_SYNC_SECRET} if CUSTOMER_SYNC_SECRET else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=CUSTOMER_SYNC_AFTER_INGEST_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8").strip()
            if not body:
                return {"status": "ok"}
            try:
                parsed = json.loads(body)
                return parsed if isinstance(parsed, dict) else {"status": "ok", "body": body}
            except Exception:
                return {"status": "ok", "body": body}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {"status": "error", "code": exc.code, "detail": detail[:1000]}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:1000]}


def _open_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(sqlite_conn: sqlite3.Connection, table: str) -> set[str]:
    cur = sqlite_conn.execute(f"PRAGMA table_info({table})")
    return {str(row["name"]) for row in cur.fetchall()}


def _sqlite_count(
    sqlite_conn: sqlite3.Connection, query: str, params: Tuple[Any, ...] = ()
) -> int:
    row = sqlite_conn.execute(query, params).fetchone()
    return int(row[0] if row is not None else 0)


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _previous_success_counts(source_system: str) -> Dict[str, int]:
    try:
        with pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_counts_json
                    FROM skimmer_import_runs
                    WHERE success = TRUE
                      AND source_system = %s
                    ORDER BY imported_at DESC
                    LIMIT 1
                    """,
                    (source_system,),
                )
                row = cur.fetchone()
    except Exception:
        return {}

    if not row:
        return {}

    payload = row.get("table_counts_json")
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, int] = {}
    for key, value in payload.items():
        try:
            out[str(key)] = int(value)
        except Exception:
            continue
    return out


def _record_pipeline_run_start_on_conn(
    conn,
    *,
    sqlite_path: str,
    source_system: str,
    source_file_sha256: str,
    trigger_reason: str,
    trigger_metadata: Optional[Dict[str, Any]],
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_pipeline_runs (
                source_filename,
                db_path,
                source_system,
                source_file_sha256,
                trigger_reason,
                trigger_metadata_json,
                success
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                os.path.basename(sqlite_path),
                sqlite_path,
                source_system,
                source_file_sha256,
                trigger_reason,
                json.dumps(trigger_metadata or {}),
                False,
            ),
        )
        run_id = int(cur.fetchone()["id"])
    conn.commit()
    return run_id


def _latest_successful_pipeline_run_by_hash(conn, *, source_system: str, source_file_sha256: str) -> Optional[Dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, completed_at, import_run_id
            FROM ingest_pipeline_runs
            WHERE source_system = %s
              AND source_file_sha256 = %s
              AND success = TRUE
            ORDER BY completed_at DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (source_system, source_file_sha256),
        )
        row = cur.fetchone()
    return row if row else None


def _record_pipeline_run_finish(
    pipeline_run_id: int,
    *,
    success: bool,
    import_run_id: Optional[int],
    validation_summary: Dict[str, Any],
    refresh_counts: Dict[str, Any],
    error_message: Optional[str] = None,
) -> None:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingest_pipeline_runs
                SET
                    completed_at = NOW(),
                    success = %s,
                    import_run_id = %s,
                    error_message = %s,
                    validation_summary_json = %s,
                    refresh_counts_json = %s
                WHERE id = %s
                """,
                (
                    success,
                    import_run_id,
                    error_message,
                    json.dumps(validation_summary),
                    json.dumps(refresh_counts),
                    pipeline_run_id,
                ),
            )
        conn.commit()


def _acquire_pipeline_lock():
    conn = pg()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (PIPELINE_LOCK_KEY,))
        row = cur.fetchone()
        locked = bool(row["locked"]) if row and "locked" in row else False
    if not locked:
        conn.close()
        raise PipelineBusyError("ingest pipeline is already running")
    return conn


def validate_sqlite_source(sqlite_path: str, source_system: str = "skimmer") -> Dict[str, Any]:
    sqlite_conn = _open_sqlite(sqlite_path)
    try:
        fatals: List[str] = []
        warnings: List[str] = []
        source_counts: Dict[str, int] = {}

        for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
            cols = _table_columns(sqlite_conn, table)
            if not cols:
                fatals.append(f"missing required table: {table}")
                continue
            missing = [column for column in required_columns if column not in cols]
            if missing:
                fatals.append(f"missing required columns in {table}: {', '.join(missing)}")

        if fatals:
            return {
                "source_system": source_system,
                "source_counts": source_counts,
                "warnings": warnings,
                "fatals": fatals,
            }

        for table in CRITICAL_COUNT_KEY_MAP:
            source_counts[table] = _sqlite_count(sqlite_conn, f"SELECT COUNT(*) FROM {table}")

        previous_counts = _previous_success_counts(source_system)
        for table, import_key in CRITICAL_COUNT_KEY_MAP.items():
            current = int(source_counts.get(table, 0))
            previous = int(previous_counts.get(import_key, 0))
            if previous > 0 and current < max(1, int(previous * FATAL_DROP_RATIO)):
                fatals.append(
                    f"catastrophic row-count drop for {table}: current={current} previous={previous} threshold_ratio={FATAL_DROP_RATIO}"
                )

        critical_join_checks = {
            "service locations without matching customers": """
                SELECT COUNT(*)
                FROM ServiceLocation sl
                LEFT JOIN Customer c ON sl.CustomerId = c.id
                WHERE sl.CustomerId IS NOT NULL AND c.id IS NULL
            """,
            "pools without matching service locations": """
                SELECT COUNT(*)
                FROM Pool p
                LEFT JOIN ServiceLocation sl ON p.ServiceLocationId = sl.id
                WHERE p.ServiceLocationId IS NOT NULL AND sl.id IS NULL
            """,
            "service stop entries without matching entry descriptions": """
                SELECT COUNT(*)
                FROM ServiceStopEntry e
                LEFT JOIN EntryDescription d ON e.EntryDescriptionId = d.id
                WHERE e.EntryDescriptionId IS NOT NULL AND d.id IS NULL
            """,
            "pool-required service stop entries missing pool ids": """
                SELECT COUNT(*)
                FROM ServiceStopEntry
                WHERE EntryType IN ('Reading', 'Dosage') AND PoolId IS NULL
            """,
            "pool-required service stop entries with broken pool joins": """
                SELECT COUNT(*)
                FROM ServiceStopEntry e
                LEFT JOIN Pool p ON e.PoolId = p.id
                WHERE e.EntryType IN ('Reading', 'Dosage')
                  AND e.PoolId IS NOT NULL
                  AND p.id IS NULL
            """,
        }
        for label, query in critical_join_checks.items():
            count = _sqlite_count(sqlite_conn, query)
            if count > 0:
                fatals.append(f"{label}: {count}")

        warning_checks = {
            "active_customer_without_pool": """
                SELECT COUNT(*)
                FROM (
                    SELECT c.id
                    FROM Customer c
                    LEFT JOIN ServiceLocation sl ON sl.CustomerId = c.id
                    LEFT JOIN Pool p ON p.ServiceLocationId = sl.id
                    WHERE COALESCE(c.IsInactive, 0) = 0
                    GROUP BY c.id
                    HAVING COUNT(DISTINCT p.id) = 0
                ) x
            """,
            "quote_only_customer_without_pool": """
                SELECT COUNT(*)
                FROM (
                    SELECT c.id
                    FROM Customer c
                    JOIN Quote q ON q.CustomerId = c.id
                    LEFT JOIN ServiceLocation sl ON sl.CustomerId = c.id
                    LEFT JOIN Pool p ON p.ServiceLocationId = sl.id
                    GROUP BY c.id
                    HAVING COUNT(DISTINCT p.id) = 0
                ) x
            """,
            "service_only_customer_without_pool": """
                SELECT COUNT(*)
                FROM (
                    SELECT c.id
                    FROM Customer c
                    JOIN ServiceLocation sl ON sl.CustomerId = c.id
                    JOIN WorkOrder w ON w.ServiceLocationId = sl.id
                    LEFT JOIN Pool p ON p.ServiceLocationId = sl.id
                    GROUP BY c.id
                    HAVING COUNT(DISTINCT p.id) = 0
                ) x
            """,
            "no_recent_service_history": """
                SELECT COUNT(*)
                FROM (
                    SELECT c.id
                    FROM Customer c
                    LEFT JOIN ServiceLocation sl ON sl.CustomerId = c.id
                    LEFT JOIN Pool p ON p.ServiceLocationId = sl.id
                    LEFT JOIN ServiceStopEntry e ON e.PoolId = p.id
                    WHERE COALESCE(c.IsInactive, 0) = 0
                    GROUP BY c.id
                    HAVING MAX(e.ServiceDate) IS NULL OR MAX(e.ServiceDate) < DATE('now', '-45 day')
                ) x
            """,
        }
        for label, query in warning_checks.items():
            count = _sqlite_count(sqlite_conn, query)
            if count > 0:
                warnings.append(f"{label}: {count}")

        return {
            "source_system": source_system,
            "source_counts": source_counts,
            "warnings": warnings,
            "fatals": fatals,
        }
    finally:
        sqlite_conn.close()


def ensure_operational_schema() -> None:
    ensure_pg_schema()
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_pipeline_runs (
                    id BIGSERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    source_filename TEXT,
                    db_path TEXT,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_file_sha256 TEXT,
                    trigger_reason TEXT NOT NULL DEFAULT 'manual',
                    trigger_metadata_json JSONB,
                    import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    success BOOLEAN NOT NULL,
                    error_message TEXT,
                    validation_summary_json JSONB,
                    refresh_counts_json JSONB
                )
                """
            )
            cur.execute(
                "ALTER TABLE ingest_pipeline_runs ADD COLUMN IF NOT EXISTS source_file_sha256 TEXT"
            )
            cur.execute(
                "ALTER TABLE ingest_pipeline_runs ADD COLUMN IF NOT EXISTS trigger_reason TEXT NOT NULL DEFAULT 'manual'"
            )
            cur.execute(
                "ALTER TABLE ingest_pipeline_runs ADD COLUMN IF NOT EXISTS trigger_metadata_json JSONB"
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_ingest_pipeline_runs_source_hash
                ON ingest_pipeline_runs(source_system, source_file_sha256)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_customer_id TEXT NOT NULL,
                    sk_customer_id BIGINT REFERENCES sk_customer(id) ON DELETE SET NULL,
                    first_name TEXT,
                    last_name TEXT,
                    company_name TEXT,
                    email TEXT,
                    phone TEXT,
                    mobile_phone TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    customer_status TEXT,
                    is_lead BOOLEAN NOT NULL DEFAULT FALSE,
                    is_inactive BOOLEAN NOT NULL DEFAULT FALSE,
                    is_operationally_active BOOLEAN NOT NULL,
                    inactive_since TIMESTAMPTZ,
                    has_pool BOOLEAN NOT NULL DEFAULT FALSE,
                    pool_count INTEGER NOT NULL DEFAULT 0,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_customer_id),
                    UNIQUE (sk_customer_id)
                )
                """
            )
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_lead BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_inactive BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS is_operationally_active BOOLEAN")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS inactive_since TIMESTAMPTZ")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS has_pool BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS pool_count INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_customers_operational_active
                ON customers(source_system, is_operationally_active)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_customers_inactive_since
                ON customers(source_system, inactive_since)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS pools (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_pool_id TEXT NOT NULL,
                    sk_pool_id BIGINT REFERENCES sk_pool(id) ON DELETE SET NULL,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    source_customer_id TEXT,
                    source_service_location_id TEXT,
                    sk_service_location_id BIGINT REFERENCES sk_service_location(id) ON DELETE SET NULL,
                    name TEXT,
                    gallons INTEGER,
                    baseline_filter_pressure NUMERIC,
                    notes TEXT,
                    equipment_items JSONB,
                    company_id TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    is_operationally_active BOOLEAN NOT NULL DEFAULT FALSE,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_pool_id),
                    UNIQUE (sk_pool_id)
                )
                """
            )
            cur.execute("ALTER TABLE pools ADD COLUMN IF NOT EXISTS is_operationally_active BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("ALTER TABLE pools ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE pools ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pools_customer
                ON pools(source_system, customer_id)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chemistry_readings (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_entry_id TEXT NOT NULL,
                    source_service_stop_id TEXT,
                    source_pool_id TEXT NOT NULL,
                    source_entry_description_id TEXT,
                    pool_id BIGINT NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    service_date TIMESTAMPTZ NOT NULL,
                    reading_key TEXT NOT NULL,
                    reading_type TEXT,
                    description TEXT,
                    unit_of_measure TEXT,
                    value NUMERIC,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_entry_id)
                )
                """
            )
            cur.execute("ALTER TABLE chemistry_readings ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE chemistry_readings ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chemistry_readings_pool_date
                ON chemistry_readings(source_system, pool_id, service_date DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chemistry_readings_key
                ON chemistry_readings(source_system, reading_key)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chemical_dose_events (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_entry_id TEXT NOT NULL,
                    source_service_stop_id TEXT,
                    source_pool_id TEXT NOT NULL,
                    source_entry_description_id TEXT,
                    pool_id BIGINT NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    service_date TIMESTAMPTZ NOT NULL,
                    dosage_key TEXT NOT NULL,
                    dosage_type TEXT,
                    description TEXT,
                    unit_of_measure TEXT,
                    quantity NUMERIC,
                    entry_cost NUMERIC,
                    entry_price NUMERIC,
                    estimated_cost NUMERIC,
                    estimated_revenue NUMERIC,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_entry_id)
                )
                """
            )
            cur.execute("ALTER TABLE chemical_dose_events ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE chemical_dose_events ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chemical_dose_events_pool_date
                ON chemical_dose_events(source_system, pool_id, service_date DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chemical_dose_events_key
                ON chemical_dose_events(source_system, dosage_key)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS technicians (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_account_id TEXT NOT NULL,
                    sk_account_id BIGINT REFERENCES sk_account(id) ON DELETE SET NULL,
                    username TEXT,
                    email TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    role_type TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT FALSE,
                    mobile_phone TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    company_id TEXT,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_account_id),
                    UNIQUE (sk_account_id)
                )
                """
            )
            cur.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE technicians ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_technicians_role_active
                ON technicians(source_system, role_type, is_active)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS service_location_technician_assignments (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_route_assignment_id TEXT NOT NULL,
                    sk_route_assignment_id BIGINT REFERENCES sk_route_assignment(id) ON DELETE SET NULL,
                    technician_id BIGINT REFERENCES technicians(id) ON DELETE CASCADE,
                    source_account_id TEXT,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    source_customer_id TEXT,
                    source_service_location_id TEXT,
                    sk_service_location_id BIGINT REFERENCES sk_service_location(id) ON DELETE SET NULL,
                    day_of_week TEXT,
                    frequency TEXT,
                    start_date TIMESTAMPTZ,
                    end_date TIMESTAMPTZ,
                    sequence INTEGER,
                    status TEXT,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    company_id TEXT,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_route_assignment_id),
                    UNIQUE (sk_route_assignment_id)
                )
                """
            )
            cur.execute("ALTER TABLE service_location_technician_assignments ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE service_location_technician_assignments ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_service_location_technician_assignments_tech
                ON service_location_technician_assignments(source_system, technician_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_service_location_technician_assignments_location_current
                ON service_location_technician_assignments(source_system, source_service_location_id, is_deleted, end_date, frequency)
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS technician_route_stops (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_route_stop_id TEXT NOT NULL,
                    sk_route_stop_id BIGINT REFERENCES sk_route_stop(id) ON DELETE SET NULL,
                    technician_id BIGINT REFERENCES technicians(id) ON DELETE CASCADE,
                    source_account_id TEXT,
                    customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
                    source_customer_id TEXT,
                    source_service_location_id TEXT,
                    sk_service_location_id BIGINT REFERENCES sk_service_location(id) ON DELETE SET NULL,
                    source_route_assignment_id TEXT,
                    service_date TIMESTAMPTZ NOT NULL,
                    start_time TIMESTAMPTZ,
                    complete_time TIMESTAMPTZ,
                    is_skipped BOOLEAN NOT NULL DEFAULT FALSE,
                    sequence INTEGER,
                    minutes_at_stop INTEGER,
                    source_route_move_id TEXT,
                    company_id TEXT,
                    last_seen_import_run_id BIGINT REFERENCES skimmer_import_runs(id) ON DELETE SET NULL,
                    last_seen_pipeline_run_id BIGINT REFERENCES ingest_pipeline_runs(id) ON DELETE SET NULL,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_route_stop_id),
                    UNIQUE (sk_route_stop_id)
                )
                """
            )
            cur.execute("ALTER TABLE technician_route_stops ADD COLUMN IF NOT EXISTS last_seen_import_run_id BIGINT")
            cur.execute("ALTER TABLE technician_route_stops ADD COLUMN IF NOT EXISTS last_seen_pipeline_run_id BIGINT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_technician_route_stops_tech_date
                ON technician_route_stops(source_system, technician_id, service_date DESC)
                """
            )

            if not INGEST_OWNS_DASHBOARD_SCHEMA:
                conn.commit()
                return
            ensure_dashboard_schema_definitions(
                conn,
                monthly_chemical_cost_review_threshold=MONTHLY_CHEMICAL_COST_REVIEW_THRESHOLD,
            )
        conn.commit()


def _upsert_customers(conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int) -> int:
    interval_literal = f"{INACTIVE_PRUNE_DAYS} days"
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH pool_counts AS (
                SELECT
                    sl.source_system,
                    sl.source_customer_id,
                    COUNT(DISTINCT p.source_pool_id)::INTEGER AS pool_count
                FROM sk_service_location sl
                LEFT JOIN sk_pool p
                  ON p.source_system = sl.source_system
                 AND p.source_service_location_id = sl.source_location_id
                WHERE sl.source_system = %s
                GROUP BY sl.source_system, sl.source_customer_id
            ),
            candidate AS (
                SELECT
                    sc.source_system,
                    sc.source_customer_id,
                    sc.id AS sk_customer_id,
                    sc.first_name,
                    sc.last_name,
                    sc.company_name,
                    sc.email,
                    sc.phone,
                    sc.mobile_phone,
                    sc.address,
                    sc.city,
                    sc.state,
                    sc.zip,
                    sc.customer_status,
                    COALESCE(sc.is_lead, FALSE) AS is_lead,
                    COALESCE(sc.is_inactive, FALSE) AS is_inactive,
                    CASE
                        WHEN COALESCE(sc.is_inactive, FALSE) THEN FALSE
                        WHEN COALESCE(sc.is_lead, FALSE) THEN TRUE
                        WHEN lower(COALESCE(sc.customer_status, '')) IN ('past', 'past_customer', 'inactive') THEN FALSE
                        WHEN lower(COALESCE(sc.customer_status, '')) IN ('active', 'customer') THEN TRUE
                        ELSE TRUE
                    END AS is_operationally_active,
                    COALESCE(pc.pool_count, 0) AS pool_count,
                    (COALESCE(pc.pool_count, 0) > 0) AS has_pool,
                    sc.raw_json,
                    existing.inactive_since AS existing_inactive_since
                FROM sk_customer sc
                LEFT JOIN pool_counts pc
                  ON pc.source_system = sc.source_system
                 AND pc.source_customer_id = sc.source_customer_id
                LEFT JOIN customers existing
                  ON existing.source_system = sc.source_system
                 AND existing.source_customer_id = sc.source_customer_id
                WHERE sc.source_system = %s
            ),
            scoped AS (
                SELECT
                    candidate.*,
                    CASE
                        WHEN candidate.is_operationally_active THEN NULL
                        WHEN candidate.existing_inactive_since IS NOT NULL THEN candidate.existing_inactive_since
                        ELSE NOW()
                    END AS next_inactive_since
                FROM candidate
            ),
            kept AS (
                SELECT *
                FROM scoped
                WHERE is_operationally_active = TRUE
                   OR next_inactive_since >= NOW() - %s::INTERVAL
            ),
            upserted AS (
                INSERT INTO customers (
                    source_system,
                    source_customer_id,
                    sk_customer_id,
                    first_name,
                    last_name,
                    company_name,
                    email,
                    phone,
                    mobile_phone,
                    address,
                    city,
                    state,
                    zip,
                    customer_status,
                    is_lead,
                    is_inactive,
                    is_operationally_active,
                    inactive_since,
                    has_pool,
                    pool_count,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_customer_id,
                    sk_customer_id,
                    first_name,
                    last_name,
                    company_name,
                    email,
                    phone,
                    mobile_phone,
                    address,
                    city,
                    state,
                    zip,
                    customer_status,
                    is_lead,
                    is_inactive,
                    is_operationally_active,
                    next_inactive_since,
                    has_pool,
                    pool_count,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM kept
                ON CONFLICT (source_system, source_customer_id) DO UPDATE
                SET
                    sk_customer_id = EXCLUDED.sk_customer_id,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    company_name = EXCLUDED.company_name,
                    email = EXCLUDED.email,
                    phone = EXCLUDED.phone,
                    mobile_phone = EXCLUDED.mobile_phone,
                    address = EXCLUDED.address,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    zip = EXCLUDED.zip,
                    customer_status = EXCLUDED.customer_status,
                    is_lead = EXCLUDED.is_lead,
                    is_inactive = EXCLUDED.is_inactive,
                    is_operationally_active = EXCLUDED.is_operationally_active,
                    inactive_since = CASE
                        WHEN EXCLUDED.is_operationally_active THEN NULL
                        WHEN customers.inactive_since IS NOT NULL AND customers.is_operationally_active = FALSE THEN customers.inactive_since
                        WHEN customers.inactive_since IS NOT NULL AND EXCLUDED.is_operationally_active = FALSE THEN customers.inactive_since
                        ELSE NOW()
                    END,
                    has_pool = EXCLUDED.has_pool,
                    pool_count = EXCLUDED.pool_count,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, source_system, interval_literal, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_customers(conn, source_system: str) -> int:
    interval_literal = f"{INACTIVE_PRUNE_DAYS} days"
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT c.id
                FROM customers c
                LEFT JOIN sk_customer sc
                  ON sc.source_system = c.source_system
                 AND sc.source_customer_id = c.source_customer_id
                WHERE c.source_system = %s
                  AND (
                      sc.id IS NULL
                      OR (
                          CASE
                              WHEN COALESCE(sc.is_inactive, FALSE) THEN FALSE
                              WHEN COALESCE(sc.is_lead, FALSE) THEN TRUE
                              WHEN lower(COALESCE(sc.customer_status, '')) IN ('past', 'past_customer', 'inactive') THEN FALSE
                              WHEN lower(COALESCE(sc.customer_status, '')) IN ('active', 'customer') THEN TRUE
                              ELSE TRUE
                          END
                      ) = FALSE
                      AND COALESCE(c.inactive_since, NOW()) < NOW() - %s::INTERVAL
                  )
            ),
            deleted AS (
                DELETE FROM customers c
                USING doomed d
                WHERE c.id = d.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system, interval_literal),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _upsert_pools(conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scoped AS (
                SELECT
                    p.source_system,
                    p.source_pool_id,
                    p.id AS sk_pool_id,
                    c.id AS customer_id,
                    sl.source_customer_id,
                    p.source_service_location_id,
                    sl.id AS sk_service_location_id,
                    p.name,
                    p.gallons,
                    p.baseline_filter_pressure,
                    p.notes,
                    p.equipment_items,
                    p.company_id,
                    sl.address,
                    sl.city,
                    sl.state,
                    sl.zip,
                    c.is_operationally_active,
                    p.raw_json
                FROM sk_pool p
                JOIN sk_service_location sl
                  ON sl.source_system = p.source_system
                 AND sl.source_location_id = p.source_service_location_id
                JOIN customers c
                  ON c.source_system = sl.source_system
                 AND c.source_customer_id = sl.source_customer_id
                WHERE p.source_system = %s
            ),
            upserted AS (
                INSERT INTO pools (
                    source_system,
                    source_pool_id,
                    sk_pool_id,
                    customer_id,
                    source_customer_id,
                    source_service_location_id,
                    sk_service_location_id,
                    name,
                    gallons,
                    baseline_filter_pressure,
                    notes,
                    equipment_items,
                    company_id,
                    address,
                    city,
                    state,
                    zip,
                    is_operationally_active,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_pool_id,
                    sk_pool_id,
                    customer_id,
                    source_customer_id,
                    source_service_location_id,
                    sk_service_location_id,
                    name,
                    gallons,
                    baseline_filter_pressure,
                    notes,
                    equipment_items,
                    company_id,
                    address,
                    city,
                    state,
                    zip,
                    is_operationally_active,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM scoped
                ON CONFLICT (source_system, source_pool_id) DO UPDATE
                SET
                    sk_pool_id = EXCLUDED.sk_pool_id,
                    customer_id = EXCLUDED.customer_id,
                    source_customer_id = EXCLUDED.source_customer_id,
                    source_service_location_id = EXCLUDED.source_service_location_id,
                    sk_service_location_id = EXCLUDED.sk_service_location_id,
                    name = EXCLUDED.name,
                    gallons = EXCLUDED.gallons,
                    baseline_filter_pressure = EXCLUDED.baseline_filter_pressure,
                    notes = EXCLUDED.notes,
                    equipment_items = EXCLUDED.equipment_items,
                    company_id = EXCLUDED.company_id,
                    address = EXCLUDED.address,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    zip = EXCLUDED.zip,
                    is_operationally_active = EXCLUDED.is_operationally_active,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_pools(conn, source_system: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT p.id
                FROM pools p
                LEFT JOIN sk_pool sp
                  ON sp.source_system = p.source_system
                 AND sp.source_pool_id = p.source_pool_id
                LEFT JOIN sk_service_location sl
                  ON sl.source_system = sp.source_system
                 AND sl.source_location_id = sp.source_service_location_id
                LEFT JOIN customers c
                  ON c.source_system = sl.source_system
                 AND c.source_customer_id = sl.source_customer_id
                WHERE p.source_system = %s
                  AND (sp.id IS NULL OR c.id IS NULL)
            ),
            deleted AS (
                DELETE FROM pools p
                USING doomed d
                WHERE p.id = d.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system,),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _upsert_chemistry_readings(
    conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scoped AS (
                SELECT
                    e.source_system,
                    e.source_entry_id,
                    e.source_service_stop_id,
                    e.source_pool_id,
                    e.source_entry_description_id,
                    p.id AS pool_id,
                    p.customer_id,
                    e.service_date,
                    normalize_metric_key(
                        COALESCE(
                            NULLIF(NULLIF(e.entry_description_text, ''), 'None'),
                            NULLIF(NULLIF(d.description, ''), 'None'),
                            NULLIF(NULLIF(e.reading_type, ''), 'None'),
                            NULLIF(NULLIF(d.reading_type, ''), 'None')
                        )
                    ) AS reading_key,
                    COALESCE(e.reading_type, d.reading_type) AS reading_type,
                    COALESCE(e.entry_description_text, d.description) AS description,
                    COALESCE(e.unit_of_measure, d.unit_of_measure) AS unit_of_measure,
                    e.value,
                    jsonb_build_object(
                        'service_stop_entry', e.raw_json,
                        'entry_description', d.raw_json
                    ) AS raw_json
                FROM sk_service_stop_entry e
                JOIN sk_entry_description d
                  ON d.source_system = e.source_system
                 AND d.source_entry_description_id = e.source_entry_description_id
                JOIN pools p
                  ON p.source_system = e.source_system
                 AND p.source_pool_id = e.source_pool_id
                WHERE e.source_system = %s
                  AND e.entry_type = 'Reading'
                  AND e.source_pool_id IS NOT NULL
            ),
            upserted AS (
                INSERT INTO chemistry_readings (
                    source_system,
                    source_entry_id,
                    source_service_stop_id,
                    source_pool_id,
                    source_entry_description_id,
                    pool_id,
                    customer_id,
                    service_date,
                    reading_key,
                    reading_type,
                    description,
                    unit_of_measure,
                    value,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_entry_id,
                    source_service_stop_id,
                    source_pool_id,
                    source_entry_description_id,
                    pool_id,
                    customer_id,
                    service_date,
                    reading_key,
                    reading_type,
                    description,
                    unit_of_measure,
                    value,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM scoped
                WHERE reading_key IS NOT NULL
                ON CONFLICT (source_system, source_entry_id) DO UPDATE
                SET
                    source_service_stop_id = EXCLUDED.source_service_stop_id,
                    source_pool_id = EXCLUDED.source_pool_id,
                    source_entry_description_id = EXCLUDED.source_entry_description_id,
                    pool_id = EXCLUDED.pool_id,
                    customer_id = EXCLUDED.customer_id,
                    service_date = EXCLUDED.service_date,
                    reading_key = EXCLUDED.reading_key,
                    reading_type = EXCLUDED.reading_type,
                    description = EXCLUDED.description,
                    unit_of_measure = EXCLUDED.unit_of_measure,
                    value = EXCLUDED.value,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_chemistry_readings(conn, source_system: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT r.id
                FROM chemistry_readings r
                LEFT JOIN sk_service_stop_entry e
                  ON e.source_system = r.source_system
                 AND e.source_entry_id = r.source_entry_id
                 AND e.entry_type = 'Reading'
                LEFT JOIN pools p ON p.id = r.pool_id
                WHERE r.source_system = %s
                  AND (e.id IS NULL OR p.id IS NULL)
            ),
            deleted AS (
                DELETE FROM chemistry_readings r
                USING doomed d
                WHERE r.id = d.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system,),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _upsert_chemical_dose_events(
    conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scoped AS (
                SELECT
                    e.source_system,
                    e.source_entry_id,
                    e.source_service_stop_id,
                    e.source_pool_id,
                    e.source_entry_description_id,
                    p.id AS pool_id,
                    p.customer_id,
                    e.service_date,
                    normalize_metric_key(COALESCE(d.dosage_type, d.description, e.entry_description_text)) AS dosage_key,
                    d.dosage_type,
                    COALESCE(e.entry_description_text, d.description) AS description,
                    COALESCE(e.unit_of_measure, d.unit_of_measure) AS unit_of_measure,
                    e.value AS quantity,
                    d.cost AS entry_cost,
                    d.price AS entry_price,
                    COALESCE(e.value, 0) * COALESCE(d.cost, 0) AS estimated_cost,
                    COALESCE(e.value, 0) * COALESCE(d.price, 0) AS estimated_revenue,
                    jsonb_build_object(
                        'service_stop_entry', e.raw_json,
                        'entry_description', d.raw_json
                    ) AS raw_json
                FROM sk_service_stop_entry e
                JOIN sk_entry_description d
                  ON d.source_system = e.source_system
                 AND d.source_entry_description_id = e.source_entry_description_id
                JOIN pools p
                  ON p.source_system = e.source_system
                 AND p.source_pool_id = e.source_pool_id
                WHERE e.source_system = %s
                  AND e.entry_type = 'Dosage'
                  AND e.source_pool_id IS NOT NULL
            ),
            upserted AS (
                INSERT INTO chemical_dose_events (
                    source_system,
                    source_entry_id,
                    source_service_stop_id,
                    source_pool_id,
                    source_entry_description_id,
                    pool_id,
                    customer_id,
                    service_date,
                    dosage_key,
                    dosage_type,
                    description,
                    unit_of_measure,
                    quantity,
                    entry_cost,
                    entry_price,
                    estimated_cost,
                    estimated_revenue,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_entry_id,
                    source_service_stop_id,
                    source_pool_id,
                    source_entry_description_id,
                    pool_id,
                    customer_id,
                    service_date,
                    dosage_key,
                    dosage_type,
                    description,
                    unit_of_measure,
                    quantity,
                    entry_cost,
                    entry_price,
                    estimated_cost,
                    estimated_revenue,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM scoped
                WHERE dosage_key IS NOT NULL
                ON CONFLICT (source_system, source_entry_id) DO UPDATE
                SET
                    source_service_stop_id = EXCLUDED.source_service_stop_id,
                    source_pool_id = EXCLUDED.source_pool_id,
                    source_entry_description_id = EXCLUDED.source_entry_description_id,
                    pool_id = EXCLUDED.pool_id,
                    customer_id = EXCLUDED.customer_id,
                    service_date = EXCLUDED.service_date,
                    dosage_key = EXCLUDED.dosage_key,
                    dosage_type = EXCLUDED.dosage_type,
                    description = EXCLUDED.description,
                    unit_of_measure = EXCLUDED.unit_of_measure,
                    quantity = EXCLUDED.quantity,
                    entry_cost = EXCLUDED.entry_cost,
                    entry_price = EXCLUDED.entry_price,
                    estimated_cost = EXCLUDED.estimated_cost,
                    estimated_revenue = EXCLUDED.estimated_revenue,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _upsert_technicians(conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scoped AS (
                SELECT
                    a.source_system,
                    a.source_account_id,
                    a.id AS sk_account_id,
                    a.username,
                    a.email,
                    a.first_name,
                    a.last_name,
                    a.role_type,
                    COALESCE(a.is_active, FALSE) AS is_active,
                    a.mobile_phone,
                    a.address,
                    a.city,
                    a.state,
                    a.zip,
                    a.company_id,
                    a.raw_json
                FROM sk_account a
                WHERE a.source_system = %s
            ),
            upserted AS (
                INSERT INTO technicians (
                    source_system,
                    source_account_id,
                    sk_account_id,
                    username,
                    email,
                    first_name,
                    last_name,
                    role_type,
                    is_active,
                    mobile_phone,
                    address,
                    city,
                    state,
                    zip,
                    company_id,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_account_id,
                    sk_account_id,
                    username,
                    email,
                    first_name,
                    last_name,
                    role_type,
                    is_active,
                    mobile_phone,
                    address,
                    city,
                    state,
                    zip,
                    company_id,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM scoped
                ON CONFLICT (source_system, source_account_id) DO UPDATE
                SET
                    sk_account_id = EXCLUDED.sk_account_id,
                    username = EXCLUDED.username,
                    email = EXCLUDED.email,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    role_type = EXCLUDED.role_type,
                    is_active = EXCLUDED.is_active,
                    mobile_phone = EXCLUDED.mobile_phone,
                    address = EXCLUDED.address,
                    city = EXCLUDED.city,
                    state = EXCLUDED.state,
                    zip = EXCLUDED.zip,
                    company_id = EXCLUDED.company_id,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_technicians(conn, source_system: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT t.id
                FROM technicians t
                LEFT JOIN sk_account a
                  ON a.source_system = t.source_system
                 AND a.source_account_id = t.source_account_id
                WHERE t.source_system = %s
                  AND a.id IS NULL
            ),
            deleted AS (
                DELETE FROM technicians t
                USING doomed d
                WHERE t.id = d.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system,),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _upsert_service_location_technician_assignments(
    conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scoped AS (
                SELECT
                    ra.source_system,
                    ra.source_route_assignment_id,
                    ra.id AS sk_route_assignment_id,
                    t.id AS technician_id,
                    ra.source_account_id,
                    c.id AS customer_id,
                    sl.source_customer_id,
                    ra.source_service_location_id,
                    sl.id AS sk_service_location_id,
                    ra.day_of_week,
                    ra.frequency,
                    ra.start_date,
                    ra.end_date,
                    ra.sequence,
                    ra.status,
                    COALESCE(ra.is_deleted, FALSE) AS is_deleted,
                    ra.company_id,
                    ra.raw_json
                FROM sk_route_assignment ra
                LEFT JOIN technicians t
                  ON t.source_system = ra.source_system
                 AND t.source_account_id = ra.source_account_id
                LEFT JOIN sk_service_location sl
                  ON sl.source_system = ra.source_system
                 AND sl.source_location_id = ra.source_service_location_id
                LEFT JOIN customers c
                  ON c.source_system = sl.source_system
                 AND c.source_customer_id = sl.source_customer_id
                WHERE ra.source_system = %s
            ),
            upserted AS (
                INSERT INTO service_location_technician_assignments (
                    source_system,
                    source_route_assignment_id,
                    sk_route_assignment_id,
                    technician_id,
                    source_account_id,
                    customer_id,
                    source_customer_id,
                    source_service_location_id,
                    sk_service_location_id,
                    day_of_week,
                    frequency,
                    start_date,
                    end_date,
                    sequence,
                    status,
                    is_deleted,
                    company_id,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_route_assignment_id,
                    sk_route_assignment_id,
                    technician_id,
                    source_account_id,
                    customer_id,
                    source_customer_id,
                    source_service_location_id,
                    sk_service_location_id,
                    day_of_week,
                    frequency,
                    start_date,
                    end_date,
                    sequence,
                    status,
                    is_deleted,
                    company_id,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM scoped
                ON CONFLICT (source_system, source_route_assignment_id) DO UPDATE
                SET
                    sk_route_assignment_id = EXCLUDED.sk_route_assignment_id,
                    technician_id = EXCLUDED.technician_id,
                    source_account_id = EXCLUDED.source_account_id,
                    customer_id = EXCLUDED.customer_id,
                    source_customer_id = EXCLUDED.source_customer_id,
                    source_service_location_id = EXCLUDED.source_service_location_id,
                    sk_service_location_id = EXCLUDED.sk_service_location_id,
                    day_of_week = EXCLUDED.day_of_week,
                    frequency = EXCLUDED.frequency,
                    start_date = EXCLUDED.start_date,
                    end_date = EXCLUDED.end_date,
                    sequence = EXCLUDED.sequence,
                    status = EXCLUDED.status,
                    is_deleted = EXCLUDED.is_deleted,
                    company_id = EXCLUDED.company_id,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_service_location_technician_assignments(conn, source_system: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT a.id
                FROM service_location_technician_assignments a
                LEFT JOIN sk_route_assignment ra
                  ON ra.source_system = a.source_system
                 AND ra.source_route_assignment_id = a.source_route_assignment_id
                WHERE a.source_system = %s
                  AND ra.id IS NULL
            ),
            deleted AS (
                DELETE FROM service_location_technician_assignments a
                USING doomed d
                WHERE a.id = d.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system,),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _upsert_technician_route_stops(
    conn, source_system: str, import_run_id: Optional[int], pipeline_run_id: int
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH scoped AS (
                SELECT
                    rs.source_system,
                    rs.source_route_stop_id,
                    rs.id AS sk_route_stop_id,
                    t.id AS technician_id,
                    rs.source_account_id,
                    c.id AS customer_id,
                    sl.source_customer_id,
                    rs.source_service_location_id,
                    sl.id AS sk_service_location_id,
                    rs.source_route_assignment_id,
                    rs.service_date,
                    rs.start_time,
                    rs.complete_time,
                    COALESCE(rs.is_skipped, FALSE) AS is_skipped,
                    rs.sequence,
                    rs.minutes_at_stop,
                    rs.source_route_move_id,
                    rs.company_id,
                    rs.raw_json
                FROM sk_route_stop rs
                LEFT JOIN technicians t
                  ON t.source_system = rs.source_system
                 AND t.source_account_id = rs.source_account_id
                LEFT JOIN sk_service_location sl
                  ON sl.source_system = rs.source_system
                 AND sl.source_location_id = rs.source_service_location_id
                LEFT JOIN customers c
                  ON c.source_system = sl.source_system
                 AND c.source_customer_id = sl.source_customer_id
                WHERE rs.source_system = %s
                  AND rs.service_date IS NOT NULL
            ),
            upserted AS (
                INSERT INTO technician_route_stops (
                    source_system,
                    source_route_stop_id,
                    sk_route_stop_id,
                    technician_id,
                    source_account_id,
                    customer_id,
                    source_customer_id,
                    source_service_location_id,
                    sk_service_location_id,
                    source_route_assignment_id,
                    service_date,
                    start_time,
                    complete_time,
                    is_skipped,
                    sequence,
                    minutes_at_stop,
                    source_route_move_id,
                    company_id,
                    last_seen_import_run_id,
                    last_seen_pipeline_run_id,
                    raw_json,
                    created_at,
                    updated_at
                )
                SELECT
                    source_system,
                    source_route_stop_id,
                    sk_route_stop_id,
                    technician_id,
                    source_account_id,
                    customer_id,
                    source_customer_id,
                    source_service_location_id,
                    sk_service_location_id,
                    source_route_assignment_id,
                    service_date,
                    start_time,
                    complete_time,
                    is_skipped,
                    sequence,
                    minutes_at_stop,
                    source_route_move_id,
                    company_id,
                    %s,
                    %s,
                    raw_json,
                    NOW(),
                    NOW()
                FROM scoped
                ON CONFLICT (source_system, source_route_stop_id) DO UPDATE
                SET
                    sk_route_stop_id = EXCLUDED.sk_route_stop_id,
                    technician_id = EXCLUDED.technician_id,
                    source_account_id = EXCLUDED.source_account_id,
                    customer_id = EXCLUDED.customer_id,
                    source_customer_id = EXCLUDED.source_customer_id,
                    source_service_location_id = EXCLUDED.source_service_location_id,
                    sk_service_location_id = EXCLUDED.sk_service_location_id,
                    source_route_assignment_id = EXCLUDED.source_route_assignment_id,
                    service_date = EXCLUDED.service_date,
                    start_time = EXCLUDED.start_time,
                    complete_time = EXCLUDED.complete_time,
                    is_skipped = EXCLUDED.is_skipped,
                    sequence = EXCLUDED.sequence,
                    minutes_at_stop = EXCLUDED.minutes_at_stop,
                    source_route_move_id = EXCLUDED.source_route_move_id,
                    company_id = EXCLUDED.company_id,
                    last_seen_import_run_id = EXCLUDED.last_seen_import_run_id,
                    last_seen_pipeline_run_id = EXCLUDED.last_seen_pipeline_run_id,
                    raw_json = EXCLUDED.raw_json,
                    updated_at = NOW()
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM upserted
            """,
            (source_system, import_run_id, pipeline_run_id),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_technician_route_stops(conn, source_system: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT s.id
                FROM technician_route_stops s
                LEFT JOIN sk_route_stop rs
                  ON rs.source_system = s.source_system
                 AND rs.source_route_stop_id = s.source_route_stop_id
                WHERE s.source_system = %s
                  AND rs.id IS NULL
            ),
            deleted AS (
                DELETE FROM technician_route_stops s
                USING doomed d
                WHERE s.id = d.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system,),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _prune_chemical_dose_events(conn, source_system: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH doomed AS (
                SELECT d.id
                FROM chemical_dose_events d
                LEFT JOIN sk_service_stop_entry e
                  ON e.source_system = d.source_system
                 AND e.source_entry_id = d.source_entry_id
                 AND e.entry_type = 'Dosage'
                LEFT JOIN pools p ON p.id = d.pool_id
                WHERE d.source_system = %s
                  AND (e.id IS NULL OR p.id IS NULL)
            ),
            deleted AS (
                DELETE FROM chemical_dose_events d
                USING doomed x
                WHERE d.id = x.id
                RETURNING 1
            )
            SELECT COUNT(*) AS count FROM deleted
            """,
            (source_system,),
        )
        row = cur.fetchone()
        return int(row["count"] if row else 0)


def _refresh_operational_tables(
    *,
    source_system: str,
    import_run_id: Optional[int],
    pipeline_run_id: int,
) -> Dict[str, int]:
    with pg() as conn:
        customer_upserts = _upsert_customers(conn, source_system, import_run_id, pipeline_run_id)
        customer_prunes = _prune_customers(conn, source_system)
        pool_upserts = _upsert_pools(conn, source_system, import_run_id, pipeline_run_id)
        pool_prunes = _prune_pools(conn, source_system)
        technician_upserts = _upsert_technicians(conn, source_system, import_run_id, pipeline_run_id)
        technician_prunes = _prune_technicians(conn, source_system)
        assignment_upserts = _upsert_service_location_technician_assignments(
            conn, source_system, import_run_id, pipeline_run_id
        )
        assignment_prunes = _prune_service_location_technician_assignments(conn, source_system)
        route_stop_upserts = _upsert_technician_route_stops(conn, source_system, import_run_id, pipeline_run_id)
        route_stop_prunes = _prune_technician_route_stops(conn, source_system)
        chemistry_upserts = _upsert_chemistry_readings(conn, source_system, import_run_id, pipeline_run_id)
        chemistry_prunes = _prune_chemistry_readings(conn, source_system)
        dose_upserts = _upsert_chemical_dose_events(conn, source_system, import_run_id, pipeline_run_id)
        dose_prunes = _prune_chemical_dose_events(conn, source_system)
        conn.commit()

    return {
        "customers_upserted": customer_upserts,
        "customers_pruned": customer_prunes,
        "pools_upserted": pool_upserts,
        "pools_pruned": pool_prunes,
        "technicians_upserted": technician_upserts,
        "technicians_pruned": technician_prunes,
        "service_location_technician_assignments_upserted": assignment_upserts,
        "service_location_technician_assignments_pruned": assignment_prunes,
        "technician_route_stops_upserted": route_stop_upserts,
        "technician_route_stops_pruned": route_stop_prunes,
        "chemistry_readings_upserted": chemistry_upserts,
        "chemistry_readings_pruned": chemistry_prunes,
        "chemical_dose_events_upserted": dose_upserts,
        "chemical_dose_events_pruned": dose_prunes,
    }


def run_pipeline(
    sqlite_path: str,
    *,
    source_system: str = "skimmer",
    trigger_reason: str = "manual",
    trigger_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f"SQLite source file not found: {sqlite_path}")

    started = time.perf_counter()
    ensure_operational_schema()
    file_hash = _file_sha256(sqlite_path)
    lock_conn = _acquire_pipeline_lock()
    pipeline_run_id: Optional[int] = None
    import_run_id: Optional[int] = None
    validation_summary: Dict[str, Any] = {}
    refresh_counts: Dict[str, Any] = {}

    try:
        if SKIP_DUPLICATE_SOURCE_SUCCESS:
            prior_success = _latest_successful_pipeline_run_by_hash(
                lock_conn,
                source_system=source_system,
                source_file_sha256=file_hash,
            )
            if prior_success:
                return {
                    "status": "skipped",
                    "reason": "matching source_file_sha256 already processed successfully",
                    "source_file_sha256": file_hash,
                    "pipeline_run_id": int(prior_success["id"]),
                    "import_run_id": prior_success.get("import_run_id"),
                    "completed_at": prior_success.get("completed_at"),
                }

        pipeline_run_id = _record_pipeline_run_start_on_conn(
            lock_conn,
            sqlite_path=sqlite_path,
            source_system=source_system,
            source_file_sha256=file_hash,
            trigger_reason=trigger_reason,
            trigger_metadata=trigger_metadata,
        )

        _log(f"validating source db={sqlite_path}")
        validation_summary = validate_sqlite_source(sqlite_path, source_system=source_system)
        fatals = validation_summary.get("fatals") or []
        if fatals:
            raise PipelineValidationError("; ".join(str(item) for item in fatals))

        _log("running source-ingest import into sk_* tables")
        import_counts = import_skimmer_data(sqlite_path, IMPORT_TABLES, source_system=source_system)
        import_run_id = int(import_counts.get("import_run_id") or 0) or None

        _log("promoting sk_* source-ingest data into normalized operational layer")
        refresh_counts = _refresh_operational_tables(
            source_system=source_system,
            import_run_id=import_run_id,
            pipeline_run_id=pipeline_run_id,
        )
        refresh_counts["import_counts"] = import_counts
        refresh_counts["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)

        _record_pipeline_run_finish(
            pipeline_run_id,
            success=True,
            import_run_id=import_run_id,
            validation_summary=validation_summary,
            refresh_counts=refresh_counts,
        )
        dashboard_refresh = _trigger_web_backend_refresh(
            pipeline_run_id=pipeline_run_id,
            source_system=source_system,
            trigger_reason=trigger_reason,
        )
        customer_sync = _trigger_customer_sync_after_ingest(
            pipeline_run_id=pipeline_run_id,
            source_system=source_system,
            trigger_reason=trigger_reason,
        )
        return {
            "status": "ok",
            "pipeline_run_id": pipeline_run_id,
            "import_run_id": import_run_id,
            "source_file_sha256": file_hash,
            "validation_summary": validation_summary,
            "refresh_counts": refresh_counts,
            "dashboard_refresh": dashboard_refresh,
            "customer_sync": customer_sync,
        }
    except Exception as exc:
        if pipeline_run_id is not None:
            _record_pipeline_run_finish(
                pipeline_run_id,
                success=False,
                import_run_id=import_run_id,
                validation_summary=validation_summary,
                refresh_counts=refresh_counts,
                error_message=str(exc),
            )
        raise
    finally:
        try:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (PIPELINE_LOCK_KEY,))
            lock_conn.commit()
        finally:
            lock_conn.close()
