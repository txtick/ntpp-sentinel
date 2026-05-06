#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

# Support being called directly (cron/subprocess) or imported from within the app.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from pg import ensure_pg_schema, pg
except ImportError:
    from app.pg import ensure_pg_schema, pg


def open_skimmer_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def row_get(row, key, default=None):
    return row[key] if key in row.keys() else default


def row_json(row):
    return json.dumps({k: row[k] for k in row.keys()})


def _normalize_tag_payload(value):
    if value is None:
        return []
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = value
    if isinstance(parsed, list):
        results = []
        seen = set()
        for item in parsed:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("Name") or "").strip()
            else:
                name = str(item).strip()
            if not name:
                continue
            name_lc = name.lower()
            if name_lc in seen:
                continue
            seen.add(name_lc)
            results.append({"name": name})
        return results
    if isinstance(parsed, dict):
        name = str(parsed.get("name") or parsed.get("Name") or "").strip()
        return [{"name": name}] if name else []
    text = str(parsed).strip()
    return [{"name": text}] if text else []


def _load_customer_tag_map(sqlite_conn):
    cur = sqlite_conn.cursor()
    cur.execute(
        """
        SELECT
            ct.CustomerId,
            t.Name
        FROM CustomerTag ct
        LEFT JOIN Tag t ON t.id = ct.TagId
        WHERE COALESCE(ct.Deleted, 0) = 0
          AND COALESCE(t.Deleted, 0) = 0
          AND t.Name IS NOT NULL
          AND trim(t.Name) <> ''
        ORDER BY ct.CustomerId, t.Name
        """
    )
    tag_map = {}
    for row in cur.fetchall():
        customer_id = row["CustomerId"]
        name = str(row["Name"] or "").strip()
        if not customer_id or not name:
            continue
        bucket = tag_map.setdefault(customer_id, [])
        if name.lower() not in {item["name"].lower() for item in bucket}:
            bucket.append({"name": name})
    return tag_map


def log(msg: str) -> None:
    print(f"[skimmer-import] {msg}")


def derive_customer_status(is_inactive, is_lead) -> str:
    if bool(is_inactive):
        return "past"
    if bool(is_lead):
        return "lead"
    return "active"


def insert_import_run(conn, source_filename, db_path):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO skimmer_import_runs (
                source_filename,
                db_path,
                success,
                error_message,
                table_counts_json,
                snapshot_date
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (source_filename, db_path, False, None, None, None),
        )
        return cur.fetchone()["id"]


def update_import_run(conn, run_id, success, error_message=None, table_counts=None):
    table_counts_json = json.dumps(table_counts) if table_counts is not None else None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE skimmer_import_runs
            SET success = %s,
                error_message = %s,
                table_counts_json = %s,
                snapshot_date = NOW()
            WHERE id = %s
            """,
            (success, error_message, table_counts_json, run_id),
        )


def upsert_customer(conn, row, source_system="skimmer", cur=None):
    source_customer_id = row["id"]
    if isinstance(row, dict):
        raw_json = json.dumps(row)
    else:
        raw_json = row_json(row)
    owns_cursor = cur is None
    if owns_cursor:
        cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO sk_customer (
                source_system,
                source_customer_id,
                first_name,
                last_name,
                company_name,
                email,
                phone,
                mobile_phone,
                is_inactive,
                is_lead,
                customer_status,
                address,
                city,
                state,
                zip,
                raw_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_system, source_customer_id)
            DO UPDATE SET
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                company_name = EXCLUDED.company_name,
                email = EXCLUDED.email,
                phone = EXCLUDED.phone,
                mobile_phone = EXCLUDED.mobile_phone,
                is_inactive = EXCLUDED.is_inactive,
                is_lead = EXCLUDED.is_lead,
                customer_status = EXCLUDED.customer_status,
                address = EXCLUDED.address,
                city = EXCLUDED.city,
                state = EXCLUDED.state,
                zip = EXCLUDED.zip,
                raw_json = EXCLUDED.raw_json,
                updated_at = NOW()
            RETURNING id
            """,
            (
                source_system,
                source_customer_id,
                row_get(row, "FirstName"),
                row_get(row, "LastName"),
                row_get(row, "CompanyName"),
                row_get(row, "PrimaryEmail"),
                row_get(row, "MobilePhone"),
                row_get(row, "MobilePhone2"),
                bool(row_get(row, "IsInactive")),
                bool(row_get(row, "IsLead")),
                derive_customer_status(row_get(row, "IsInactive"), row_get(row, "IsLead")),
                row_get(row, "BillingAddress"),
                row_get(row, "BillingCity"),
                row_get(row, "BillingState"),
                row_get(row, "BillingZip"),
                raw_json,
            ),
        )
        return cur.fetchone()["id"]
    finally:
        if owns_cursor:
            cur.close()


def upsert_identity_map(conn, sk_customer_id, source_id, identity_type, source_system="skimmer", cur=None):
    if not source_id:
        return
    owns_cursor = cur is None
    if owns_cursor:
        cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO customer_identity_map (
                sk_customer_id,
                source_system,
                source_id,
                identity_type
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_system, source_id, identity_type)
            DO NOTHING
            """,
            (sk_customer_id, source_system, source_id, identity_type),
        )
    finally:
        if owns_cursor:
            cur.close()


ENTRY_DESCRIPTION_UPSERT_SQL = """
    INSERT INTO sk_entry_description (
        source_system,
        source_entry_description_id,
        description,
        unit_of_measure,
        entry_type,
        reading_type,
        dosage_type,
        selected_index,
        sequence,
        cost,
        price,
        can_include_with_service,
        column_sequence,
        company_id,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_entry_description_id)
    DO UPDATE SET
        description = EXCLUDED.description,
        unit_of_measure = EXCLUDED.unit_of_measure,
        entry_type = EXCLUDED.entry_type,
        reading_type = EXCLUDED.reading_type,
        dosage_type = EXCLUDED.dosage_type,
        selected_index = EXCLUDED.selected_index,
        sequence = EXCLUDED.sequence,
        cost = EXCLUDED.cost,
        price = EXCLUDED.price,
        can_include_with_service = EXCLUDED.can_include_with_service,
        column_sequence = EXCLUDED.column_sequence,
        company_id = EXCLUDED.company_id,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


SERVICE_STOP_ENTRY_UPSERT_SQL = """
    INSERT INTO sk_service_stop_entry (
        source_system,
        source_entry_id,
        source_service_stop_id,
        source_pool_id,
        source_entry_description_id,
        entry_type,
        value,
        service_date,
        company_id,
        entry_description_text,
        unit_of_measure,
        reading_type,
        selected_index,
        sequence,
        value_display,
        work_order_id,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_entry_id)
    DO UPDATE SET
        source_service_stop_id = EXCLUDED.source_service_stop_id,
        source_pool_id = EXCLUDED.source_pool_id,
        source_entry_description_id = EXCLUDED.source_entry_description_id,
        entry_type = EXCLUDED.entry_type,
        value = EXCLUDED.value,
        service_date = EXCLUDED.service_date,
        company_id = EXCLUDED.company_id,
        entry_description_text = EXCLUDED.entry_description_text,
        unit_of_measure = EXCLUDED.unit_of_measure,
        reading_type = EXCLUDED.reading_type,
        selected_index = EXCLUDED.selected_index,
        sequence = EXCLUDED.sequence,
        value_display = EXCLUDED.value_display,
        work_order_id = EXCLUDED.work_order_id,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


POOL_UPSERT_SQL = """
    INSERT INTO sk_pool (
        source_system,
        source_pool_id,
        source_service_location_id,
        name,
        gallons,
        baseline_filter_pressure,
        notes,
        equipment_items,
        company_id,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_pool_id)
    DO UPDATE SET
        source_service_location_id = EXCLUDED.source_service_location_id,
        name = EXCLUDED.name,
        gallons = EXCLUDED.gallons,
        baseline_filter_pressure = EXCLUDED.baseline_filter_pressure,
        notes = EXCLUDED.notes,
        equipment_items = EXCLUDED.equipment_items,
        company_id = EXCLUDED.company_id,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


SERVICE_LOCATION_UPSERT_SQL = """
    INSERT INTO sk_service_location (
        source_system,
        source_location_id,
        source_customer_id,
        address,
        city,
        state,
        zip,
        location_code,
        latitude,
        longitude,
        minutes_at_stop,
        is_bad_address,
        gate_code,
        dogs_name,
        notes,
        rate,
        rate_type,
        labor_cost,
        labor_cost_type,
        pools,
        route_assignments,
        route_moves,
        work_order_models,
        recurring_work_items,
        company_id,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_location_id)
    DO UPDATE SET
        source_customer_id = EXCLUDED.source_customer_id,
        address = EXCLUDED.address,
        city = EXCLUDED.city,
        state = EXCLUDED.state,
        zip = EXCLUDED.zip,
        location_code = EXCLUDED.location_code,
        latitude = EXCLUDED.latitude,
        longitude = EXCLUDED.longitude,
        minutes_at_stop = EXCLUDED.minutes_at_stop,
        is_bad_address = EXCLUDED.is_bad_address,
        gate_code = EXCLUDED.gate_code,
        dogs_name = EXCLUDED.dogs_name,
        notes = EXCLUDED.notes,
        rate = EXCLUDED.rate,
        rate_type = EXCLUDED.rate_type,
        labor_cost = EXCLUDED.labor_cost,
        labor_cost_type = EXCLUDED.labor_cost_type,
        pools = EXCLUDED.pools,
        route_assignments = EXCLUDED.route_assignments,
        route_moves = EXCLUDED.route_moves,
        work_order_models = EXCLUDED.work_order_models,
        recurring_work_items = EXCLUDED.recurring_work_items,
        company_id = EXCLUDED.company_id,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


ACCOUNT_UPSERT_SQL = """
    INSERT INTO sk_account (
        source_system,
        source_account_id,
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
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_account_id)
    DO UPDATE SET
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
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


ROUTE_ASSIGNMENT_UPSERT_SQL = """
    INSERT INTO sk_route_assignment (
        source_system,
        source_route_assignment_id,
        source_service_location_id,
        source_account_id,
        day_of_week,
        frequency,
        start_date,
        end_date,
        sequence,
        company_id,
        status,
        is_deleted,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_route_assignment_id)
    DO UPDATE SET
        source_service_location_id = EXCLUDED.source_service_location_id,
        source_account_id = EXCLUDED.source_account_id,
        day_of_week = EXCLUDED.day_of_week,
        frequency = EXCLUDED.frequency,
        start_date = EXCLUDED.start_date,
        end_date = EXCLUDED.end_date,
        sequence = EXCLUDED.sequence,
        company_id = EXCLUDED.company_id,
        status = EXCLUDED.status,
        is_deleted = EXCLUDED.is_deleted,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


ROUTE_STOP_UPSERT_SQL = """
    INSERT INTO sk_route_stop (
        source_system,
        source_route_stop_id,
        source_account_id,
        source_service_location_id,
        source_route_assignment_id,
        service_date,
        start_time,
        complete_time,
        is_skipped,
        skipped_stop_reason_id,
        is_off_biweekly,
        sequence,
        minutes_at_stop,
        source_route_move_id,
        email_status,
        email_sent_date,
        company_id,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_route_stop_id)
    DO UPDATE SET
        source_account_id = EXCLUDED.source_account_id,
        source_service_location_id = EXCLUDED.source_service_location_id,
        source_route_assignment_id = EXCLUDED.source_route_assignment_id,
        service_date = EXCLUDED.service_date,
        start_time = EXCLUDED.start_time,
        complete_time = EXCLUDED.complete_time,
        is_skipped = EXCLUDED.is_skipped,
        skipped_stop_reason_id = EXCLUDED.skipped_stop_reason_id,
        is_off_biweekly = EXCLUDED.is_off_biweekly,
        sequence = EXCLUDED.sequence,
        minutes_at_stop = EXCLUDED.minutes_at_stop,
        source_route_move_id = EXCLUDED.source_route_move_id,
        email_status = EXCLUDED.email_status,
        email_sent_date = EXCLUDED.email_sent_date,
        company_id = EXCLUDED.company_id,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


WORK_ORDER_TYPE_UPSERT_SQL = """
    INSERT INTO sk_work_order_type (
        source_system,
        source_work_order_type_id,
        description,
        default_work_needed,
        default_work_performed,
        default_minutes,
        default_labor_cost,
        default_price,
        default_email_subject,
        default_email_header,
        default_email_message,
        company_id,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_work_order_type_id)
    DO UPDATE SET
        description = EXCLUDED.description,
        default_work_needed = EXCLUDED.default_work_needed,
        default_work_performed = EXCLUDED.default_work_performed,
        default_minutes = EXCLUDED.default_minutes,
        default_labor_cost = EXCLUDED.default_labor_cost,
        default_price = EXCLUDED.default_price,
        default_email_subject = EXCLUDED.default_email_subject,
        default_email_header = EXCLUDED.default_email_header,
        default_email_message = EXCLUDED.default_email_message,
        company_id = EXCLUDED.company_id,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


WORK_ORDER_UPSERT_SQL = """
    INSERT INTO sk_work_order (
        source_system,
        source_work_order_id,
        source_work_order_type_id,
        source_service_location_id,
        work_needed,
        work_performed,
        source_added_by_account_id,
        added_on_date,
        source_account_id,
        service_date,
        scheduled_time,
        start_time,
        complete_time,
        route_sequence,
        estimated_minutes,
        labor_cost,
        price,
        is_invoiced,
        sync_date,
        email_header,
        email_message,
        email_status,
        email_sent_date,
        company_id,
        notes,
        note_is_alert,
        note_is_handled,
        is_deleted,
        raw_json
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (source_system, source_work_order_id)
    DO UPDATE SET
        source_work_order_type_id = EXCLUDED.source_work_order_type_id,
        source_service_location_id = EXCLUDED.source_service_location_id,
        work_needed = EXCLUDED.work_needed,
        work_performed = EXCLUDED.work_performed,
        source_added_by_account_id = EXCLUDED.source_added_by_account_id,
        added_on_date = EXCLUDED.added_on_date,
        source_account_id = EXCLUDED.source_account_id,
        service_date = EXCLUDED.service_date,
        scheduled_time = EXCLUDED.scheduled_time,
        start_time = EXCLUDED.start_time,
        complete_time = EXCLUDED.complete_time,
        route_sequence = EXCLUDED.route_sequence,
        estimated_minutes = EXCLUDED.estimated_minutes,
        labor_cost = EXCLUDED.labor_cost,
        price = EXCLUDED.price,
        is_invoiced = EXCLUDED.is_invoiced,
        sync_date = EXCLUDED.sync_date,
        email_header = EXCLUDED.email_header,
        email_message = EXCLUDED.email_message,
        email_status = EXCLUDED.email_status,
        email_sent_date = EXCLUDED.email_sent_date,
        company_id = EXCLUDED.company_id,
        notes = EXCLUDED.notes,
        note_is_alert = EXCLUDED.note_is_alert,
        note_is_handled = EXCLUDED.note_is_handled,
        is_deleted = EXCLUDED.is_deleted,
        raw_json = EXCLUDED.raw_json,
        updated_at = NOW()
"""


_IMPORT_CHUNK_SIZE = max(1, int(os.getenv("SKIMMER_IMPORT_CHUNK_SIZE", "500")))

TABLE_NAME_ALIASES = {
    "customer": "customers",
    "customers": "customers",
    "pool": "pools",
    "pools": "pools",
    "location": "locations",
    "locations": "locations",
    "servicelocation": "locations",
    "service_location": "locations",
    "account": "accounts",
    "accounts": "accounts",
    "routeassignment": "route_assignments",
    "route_assignments": "route_assignments",
    "routestop": "route_stops",
    "route_stops": "route_stops",
    "entrydescription": "entry_descriptions",
    "entry_descriptions": "entry_descriptions",
    "servicestopentry": "service_stop_entries",
    "service_stop_entries": "service_stop_entries",
    "workorder": "work_orders",
    "workorders": "work_orders",
    "work_orders": "work_orders",
    "workordertype": "work_order_types",
    "workordertypes": "work_order_types",
    "work_order_type": "work_order_types",
    "work_order_types": "work_order_types",
}


def _iter_sqlite_rows(sqlite_conn, query: str):
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(query)
    while True:
        rows = sqlite_cur.fetchmany(_IMPORT_CHUNK_SIZE)
        if not rows:
            break
        yield rows


def _executemany_upsert(pg_conn, sql: str, params_batch) -> int:
    with pg_conn.cursor() as cur:
        cur.executemany(sql, params_batch)
    return len(params_batch)


def _pool_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "ServiceLocationId"),
        row_get(row, "Name"),
        row_get(row, "Gallons"),
        row_get(row, "BaselineFilterPressure"),
        row_get(row, "Notes"),
        row_get(row, "EquipmentItems"),
        row_get(row, "CompanyId"),
        row_json(row),
    )


def _service_location_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "CustomerId"),
        row_get(row, "Address"),
        row_get(row, "City"),
        row_get(row, "State"),
        row_get(row, "Zip"),
        row_get(row, "LocationCode"),
        row_get(row, "Latitude"),
        row_get(row, "Longitude"),
        row_get(row, "MinutesAtStop"),
        bool(row_get(row, "IsBadAddress")),
        row_get(row, "GateCode"),
        row_get(row, "DogsName"),
        row_get(row, "Notes"),
        row_get(row, "Rate"),
        row_get(row, "RateType"),
        row_get(row, "LaborCost"),
        row_get(row, "LaborCostType"),
        row_get(row, "Pools"),
        row_get(row, "RouteAssignments"),
        row_get(row, "RouteMoves"),
        row_get(row, "WorkOrderModels"),
        row_get(row, "RecurringWorkItems"),
        row_get(row, "CompanyId"),
        row_json(row),
    )


def _account_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "Username"),
        row_get(row, "Email"),
        row_get(row, "FirstName"),
        row_get(row, "LastName"),
        row_get(row, "RoleType"),
        bool(row_get(row, "IsActive")),
        row_get(row, "MobilePhone"),
        row_get(row, "Address"),
        row_get(row, "City"),
        row_get(row, "State"),
        row_get(row, "Zip"),
        row_get(row, "CompanyId"),
        row_json(row),
    )


def _route_assignment_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "ServiceLocationId"),
        row_get(row, "AccountId"),
        row_get(row, "DayOfWeek"),
        row_get(row, "Frequency"),
        row_get(row, "StartDate"),
        row_get(row, "EndDate"),
        row_get(row, "Sequence"),
        row_get(row, "CompanyId"),
        row_get(row, "Status"),
        bool(row_get(row, "Deleted")),
        row_json(row),
    )


def _route_stop_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "AccountId"),
        row_get(row, "ServiceLocationId"),
        row_get(row, "RouteAssignmentId"),
        row_get(row, "ServiceDate"),
        row_get(row, "StartTime"),
        row_get(row, "CompleteTime"),
        bool(row_get(row, "IsSkipped")),
        row_get(row, "SkippedStopReasonId"),
        bool(row_get(row, "IsOffBiweekly")),
        row_get(row, "Sequence"),
        row_get(row, "MinutesAtStop"),
        row_get(row, "RouteMoveId"),
        row_get(row, "EmailStatus"),
        row_get(row, "EmailSentDate"),
        row_get(row, "CompanyId"),
        row_json(row),
    )


def _entry_description_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "Description"),
        row_get(row, "UnitOfMeasure"),
        row_get(row, "EntryType"),
        row_get(row, "ReadingType"),
        row_get(row, "DosageType"),
        row_get(row, "SelectedIndex"),
        row_get(row, "Sequence"),
        row_get(row, "Cost"),
        row_get(row, "Price"),
        bool(row_get(row, "CanIncludeWithService")),
        row_get(row, "ColumnSequence"),
        row_get(row, "CompanyId"),
        row_json(row),
    )


def _service_stop_entry_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "ServiceStopId"),
        row_get(row, "PoolId"),
        row_get(row, "EntryDescriptionId"),
        row_get(row, "EntryType"),
        row_get(row, "Value"),
        row_get(row, "ServiceDate"),
        row_get(row, "CompanyId"),
        row_get(row, "EntryDescriptionText"),
        row_get(row, "UnitOfMeasure"),
        row_get(row, "ReadingType"),
        row_get(row, "SelectedIndex"),
        row_get(row, "Sequence"),
        row_get(row, "ValueDisplay"),
        row_get(row, "WorkOrderId"),
        row_json(row),
    )


def _work_order_type_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "Description"),
        row_get(row, "DefaultWorkNeeded"),
        row_get(row, "DefaultWorkPerformed"),
        row_get(row, "DefaultMinutes"),
        row_get(row, "DefaultLaborCost"),
        row_get(row, "DefaultPrice"),
        row_get(row, "DefaultEmailSubject"),
        row_get(row, "DefaultEmailHeader"),
        row_get(row, "DefaultEmailMessage"),
        row_get(row, "CompanyId"),
        row_json(row),
    )


def _work_order_params(row, source_system="skimmer"):
    return (
        source_system,
        row["id"],
        row_get(row, "WorkOrderTypeId"),
        row_get(row, "ServiceLocationId"),
        row_get(row, "WorkNeeded"),
        row_get(row, "WorkPerformed"),
        row_get(row, "AddedByAccountId"),
        row_get(row, "AddedOnDate"),
        row_get(row, "AccountId"),
        row_get(row, "ServiceDate"),
        row_get(row, "ScheduledTime"),
        row_get(row, "StartTime"),
        row_get(row, "CompleteTime"),
        row_get(row, "RouteSequence"),
        row_get(row, "EstimatedMinutes"),
        row_get(row, "LaborCost"),
        row_get(row, "Price"),
        bool(row_get(row, "IsInvoiced")),
        row_get(row, "SyncDate"),
        row_get(row, "EmailHeader"),
        row_get(row, "EmailMessage"),
        row_get(row, "EmailStatus"),
        row_get(row, "EmailSentDate"),
        row_get(row, "CompanyId"),
        row_get(row, "Notes"),
        bool(row_get(row, "NoteIsAlert")),
        bool(row_get(row, "NoteIsHandled")),
        bool(row_get(row, "Deleted")),
        row_json(row),
    )


def import_pools(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, ServiceLocationId, Name, Gallons, BaselineFilterPressure, Notes, EquipmentItems, CompanyId FROM Pool"
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            POOL_UPSERT_SQL,
            [_pool_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"pools chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(f"pools imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}")
    return imported


def import_service_locations(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, CustomerId, Address, City, State, Zip, LocationCode, Latitude, Longitude, MinutesAtStop, IsBadAddress, GateCode, DogsName, Notes, Rate, RateType, LaborCost, LaborCostType, Pools, RouteAssignments, RouteMoves, WorkOrderModels, RecurringWorkItems, CompanyId FROM ServiceLocation"
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            SERVICE_LOCATION_UPSERT_SQL,
            [_service_location_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"service_locations chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"service_locations imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_entry_descriptions(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, Description, UnitOfMeasure, EntryType, ReadingType, DosageType, SelectedIndex, Sequence, Cost, Price, CanIncludeWithService, ColumnSequence, CompanyId FROM EntryDescription"
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            ENTRY_DESCRIPTION_UPSERT_SQL,
            [_entry_description_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"entry_descriptions chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"entry_descriptions imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_accounts(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, Username, Email, FirstName, LastName, RoleType, IsActive, MobilePhone, Address, City, State, Zip, CompanyId FROM Account",
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            ACCOUNT_UPSERT_SQL,
            [_account_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"accounts chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(f"accounts imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}")
    return imported


def import_route_assignments(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, ServiceLocationId, AccountId, DayOfWeek, Frequency, StartDate, EndDate, Sequence, CompanyId, Status, Deleted FROM RouteAssignment",
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            ROUTE_ASSIGNMENT_UPSERT_SQL,
            [_route_assignment_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"route_assignments chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"route_assignments imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_route_stops(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, AccountId, ServiceLocationId, RouteAssignmentId, ServiceDate, StartTime, CompleteTime, IsSkipped, SkippedStopReasonId, IsOffBiweekly, Sequence, MinutesAtStop, RouteMoveId, EmailStatus, EmailSentDate, CompanyId FROM RouteStop",
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            ROUTE_STOP_UPSERT_SQL,
            [_route_stop_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"route_stops chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"route_stops imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_work_order_types(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, Description, DefaultWorkNeeded, DefaultWorkPerformed, DefaultMinutes, DefaultLaborCost, DefaultPrice, DefaultEmailSubject, DefaultEmailHeader, DefaultEmailMessage, CompanyId FROM WorkOrderType",
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            WORK_ORDER_TYPE_UPSERT_SQL,
            [_work_order_type_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"work_order_types chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"work_order_types imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_work_orders(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, WorkOrderTypeId, ServiceLocationId, WorkNeeded, WorkPerformed, AddedByAccountId, AddedOnDate, AccountId, ServiceDate, ScheduledTime, StartTime, CompleteTime, RouteSequence, EstimatedMinutes, LaborCost, Price, IsInvoiced, SyncDate, EmailHeader, EmailMessage, EmailStatus, EmailSentDate, CompanyId, Notes, NoteIsAlert, NoteIsHandled, Deleted FROM WorkOrder",
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            WORK_ORDER_UPSERT_SQL,
            [_work_order_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"work_orders chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"work_orders imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_service_stop_entries(pg_conn, sqlite_conn, source_system="skimmer"):
    imported = 0
    started_at = time.perf_counter()
    chunk_count = 0
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, ServiceStopId, PoolId, EntryDescriptionId, EntryType, Value, ServiceDate, CompanyId, EntryDescriptionText, UnitOfMeasure, ReadingType, SelectedIndex, Sequence, ValueDisplay, WorkOrderId FROM ServiceStopEntry"
    ):
        chunk_started_at = time.perf_counter()
        imported += _executemany_upsert(
            pg_conn,
            SERVICE_STOP_ENTRY_UPSERT_SQL,
            [_service_stop_entry_params(row, source_system=source_system) for row in rows],
        )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"service_stop_entries chunk={chunk_count} rows={len(rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"service_stop_entries imported={imported} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported


def import_customers(sqlite_conn, pg_conn, source_system="skimmer"):
    imported = 0
    identity_count = 0
    started_at = time.perf_counter()
    chunk_count = 0
    customer_tag_map = _load_customer_tag_map(sqlite_conn)
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, FirstName, LastName, CompanyName, PrimaryEmail, MobilePhone, MobilePhone2, BillingAddress, BillingCity, BillingState, BillingZip, IsInactive, IsLead, Tags FROM Customer",
    ):
        chunk_started_at = time.perf_counter()
        identity_rows = []
        with pg_conn.cursor() as customer_cur:
            for row in rows:
                payload = {k: row[k] for k in row.keys()}
                merged_tags = _normalize_tag_payload(payload.get("Tags"))
                for item in customer_tag_map.get(str(payload.get("id") or ""), []):
                    name = str(item.get("name") or "").strip()
                    if not name:
                        continue
                    if name.lower() in {existing["name"].lower() for existing in merged_tags}:
                        continue
                    merged_tags.append({"name": name})
                if merged_tags:
                    payload["Tags"] = merged_tags
                sk_customer_id = upsert_customer(pg_conn, payload, source_system=source_system, cur=customer_cur)
                imported += 1
                for source_id, identity_type in (
                    (row_get(row, "id"), "customer_id"),
                    (row_get(row, "PrimaryEmail"), "email"),
                    (row_get(row, "MobilePhone"), "mobile_phone"),
                    (row_get(row, "MobilePhone2"), "mobile_phone"),
                ):
                    if source_id:
                        identity_rows.append((sk_customer_id, source_system, source_id, identity_type))
                        identity_count += 1
        if identity_rows:
            with pg_conn.cursor() as identity_cur:
                identity_cur.executemany(
                    """
                    INSERT INTO customer_identity_map (
                        sk_customer_id,
                        source_system,
                        source_id,
                        identity_type
                    ) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (source_system, source_id, identity_type)
                    DO NOTHING
                    """,
                    identity_rows,
                )
        pg_conn.commit()
        chunk_count += 1
        log(
            f"customers chunk={chunk_count} rows={len(rows)} identities={len(identity_rows)} elapsed_ms={round((time.perf_counter() - chunk_started_at) * 1000, 1)}"
        )
    log(
        f"customers imported={imported} identities={identity_count} chunks={chunk_count} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return imported, identity_count


def import_skimmer_data(sqlite_path, tables, source_system="skimmer"):
    started_at = time.perf_counter()
    ensure_pg_schema()
    normalized_tables = []
    for raw_name in tables:
        key = str(raw_name).strip().lower()
        if not key:
            continue
        normalized_tables.append(TABLE_NAME_ALIASES.get(key, key))
    sqlite_conn = open_skimmer_sqlite(sqlite_path)
    with pg() as pg_conn:
        run_id = insert_import_run(pg_conn, os.path.basename(sqlite_path), sqlite_path)
        counts = {}
        success = True
        error_message = None
        try:
            if "customers" in normalized_tables:
                imported, identity_count = import_customers(sqlite_conn, pg_conn, source_system=source_system)
                counts["customers_imported"] = imported
                counts["identity_records"] = identity_count
            if "pools" in normalized_tables:
                counts["pools_imported"] = import_pools(pg_conn, sqlite_conn, source_system=source_system)
            if "locations" in normalized_tables:
                counts["service_locations_imported"] = import_service_locations(pg_conn, sqlite_conn, source_system=source_system)
            if "accounts" in normalized_tables:
                counts["accounts_imported"] = import_accounts(pg_conn, sqlite_conn, source_system=source_system)
            if "route_assignments" in normalized_tables:
                counts["route_assignments_imported"] = import_route_assignments(pg_conn, sqlite_conn, source_system=source_system)
            if "route_stops" in normalized_tables:
                counts["route_stops_imported"] = import_route_stops(pg_conn, sqlite_conn, source_system=source_system)
            if "work_order_types" in normalized_tables:
                counts["work_order_types_imported"] = import_work_order_types(pg_conn, sqlite_conn, source_system=source_system)
            if "work_orders" in normalized_tables:
                counts["work_orders_imported"] = import_work_orders(pg_conn, sqlite_conn, source_system=source_system)
            if "entry_descriptions" in normalized_tables:
                counts["entry_descriptions_imported"] = import_entry_descriptions(pg_conn, sqlite_conn, source_system=source_system)
            if "service_stop_entries" in normalized_tables:
                counts["service_stop_entries_imported"] = import_service_stop_entries(pg_conn, sqlite_conn, source_system=source_system)
            pg_conn.commit()
        except Exception as exc:
            pg_conn.rollback()
            success = False
            error_message = str(exc)
            raise
        finally:
            update_import_run(pg_conn, run_id, success, error_message=error_message, table_counts=counts)
            pg_conn.commit()
    sqlite_conn.close()
    log(
        f"import complete tables={','.join(normalized_tables)} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    counts["import_run_id"] = run_id
    return counts


def parse_args():
    parser = argparse.ArgumentParser(description="Import Customer data from a Skimmer SQLite export into Postgres.")
    parser.add_argument("--sqlite", default=os.getenv("SKIMMER_DB_PATH"), help="Path to the Skimmer SQLite export file.")
    parser.add_argument(
        "--tables",
        default="customers",
        help="Comma-separated tables to import: customers,pools,locations,accounts,route_assignments,route_stops,work_order_types,work_orders,entry_descriptions,service_stop_entries,all",
    )
    parser.add_argument("--source-system", default="skimmer", help="Source system identifier.")
    return parser.parse_args()


def main():
    args = parse_args()
    if not args.sqlite:
        print("ERROR: missing SQLite source path. Set SKIMMER_DB_PATH or pass --sqlite.")
        sys.exit(1)
    if not os.path.exists(args.sqlite):
        print(f"ERROR: SQLite source file not found: {args.sqlite}")
        sys.exit(1)

    tables = [t.strip().lower() for t in args.tables.split(",") if t.strip()]
    if not tables:
        tables = ["customers"]
    if "all" in tables:
        tables = [
            "customers",
            "locations",
            "pools",
            "accounts",
            "route_assignments",
            "route_stops",
            "work_order_types",
            "work_orders",
            "entry_descriptions",
            "service_stop_entries",
        ]

    counts = import_skimmer_data(args.sqlite, tables, source_system=args.source_system)
    print(f"Imported {counts} from {args.sqlite}")


if __name__ == "__main__":
    main()
