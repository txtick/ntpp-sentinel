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
    from pg import pg
except ImportError:
    from app.pg import pg


def open_skimmer_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def row_get(row, key, default=None):
    return row[key] if key in row.keys() else default


def row_json(row):
    return json.dumps({k: row[k] for k in row.keys()})


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


_IMPORT_CHUNK_SIZE = max(1, int(os.getenv("SKIMMER_IMPORT_CHUNK_SIZE", "500")))


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
    for rows in _iter_sqlite_rows(
        sqlite_conn,
        "SELECT id, FirstName, LastName, CompanyName, PrimaryEmail, MobilePhone, MobilePhone2, BillingAddress, BillingCity, BillingState, BillingZip, IsInactive, IsLead FROM Customer",
    ):
        chunk_started_at = time.perf_counter()
        identity_rows = []
        with pg_conn.cursor() as customer_cur:
            for row in rows:
                sk_customer_id = upsert_customer(pg_conn, row, source_system=source_system, cur=customer_cur)
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
    sqlite_conn = open_skimmer_sqlite(sqlite_path)
    with pg() as pg_conn:
        run_id = insert_import_run(pg_conn, os.path.basename(sqlite_path), sqlite_path)
        counts = {}
        success = True
        error_message = None
        try:
            if "customers" in tables:
                imported, identity_count = import_customers(sqlite_conn, pg_conn, source_system=source_system)
                counts["customers_imported"] = imported
                counts["identity_records"] = identity_count
            if "pools" in tables:
                counts["pools_imported"] = import_pools(pg_conn, sqlite_conn, source_system=source_system)
            if "locations" in tables:
                counts["service_locations_imported"] = import_service_locations(pg_conn, sqlite_conn, source_system=source_system)
            if "entry_descriptions" in tables:
                counts["entry_descriptions_imported"] = import_entry_descriptions(pg_conn, sqlite_conn, source_system=source_system)
            if "service_stop_entries" in tables:
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
        f"import complete tables={','.join(tables)} total_elapsed_ms={round((time.perf_counter() - started_at) * 1000, 1)}"
    )
    return counts


def parse_args():
    parser = argparse.ArgumentParser(description="Import Customer data from a Skimmer SQLite export into Postgres.")
    parser.add_argument("--sqlite", default=os.getenv("SKIMMER_DB_PATH"), help="Path to the Skimmer SQLite export file.")
    parser.add_argument(
        "--tables",
        default="customers",
        help="Comma-separated tables to import: customers,pools,locations,entry_descriptions,service_stop_entries,all",
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
        tables = ["customers", "pools", "locations", "entry_descriptions", "service_stop_entries"]

    counts = import_skimmer_data(args.sqlite, tables, source_system=args.source_system)
    print(f"Imported {counts} from {args.sqlite}")


if __name__ == "__main__":
    main()
