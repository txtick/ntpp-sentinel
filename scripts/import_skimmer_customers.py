#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
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


def upsert_customer(conn, row, source_system="skimmer"):
    source_customer_id = row["id"]
    raw_json = json.dumps({k: row[k] for k in row.keys()})
    with conn.cursor() as cur:
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
                address,
                city,
                state,
                zip,
                raw_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_system, source_customer_id)
            DO UPDATE SET
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
                row_get(row, "BillingAddress"),
                row_get(row, "BillingCity"),
                row_get(row, "BillingState"),
                row_get(row, "BillingZip"),
                raw_json,
            ),
        )
        return cur.fetchone()["id"]


def upsert_identity_map(conn, sk_customer_id, source_id, identity_type, source_system="skimmer"):
    if not source_id:
        return
    with conn.cursor() as cur:
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


def upsert_entry_description(conn, row, source_system="skimmer"):
    source_entry_description_id = row["id"]
    raw_json = json.dumps({k: row[k] for k in row.keys()})
    with conn.cursor() as cur:
        cur.execute(
            """
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
            RETURNING id
            """,
            (
                source_system,
                source_entry_description_id,
                row_get(row, "Description"),
                row_get(row, "UnitOfMeasure"),
                row_get(row, "EntryType"),
                row_get(row, "ReadingType"),
                row_get(row, "DosageType"),
                row_get(row, "SelectedIndex"),
                row_get(row, "Sequence"),
                row_get(row, "Cost"),
                row_get(row, "Price"),
                row_get(row, "CanIncludeWithService"),
                row_get(row, "ColumnSequence"),
                row_get(row, "CompanyId"),
                raw_json,
            ),
        )
        return cur.fetchone()["id"]


def upsert_service_stop_entry(conn, row, source_system="skimmer"):
    source_entry_id = row["id"]
    raw_json = json.dumps({k: row[k] for k in row.keys()})
    with conn.cursor() as cur:
        cur.execute(
            """
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
            RETURNING id
            """,
            (
                source_system,
                source_entry_id,
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
                raw_json,
            ),
        )
        return cur.fetchone()["id"]


def upsert_pool(conn, row, source_system="skimmer"):
    source_pool_id = row["id"]
    raw_json = json.dumps({k: row[k] for k in row.keys()})
    with conn.cursor() as cur:
        cur.execute(
            """
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
            RETURNING id
            """,
            (
                source_system,
                source_pool_id,
                row_get(row, "ServiceLocationId"),
                row_get(row, "Name"),
                row_get(row, "Gallons"),
                row_get(row, "BaselineFilterPressure"),
                row_get(row, "Notes"),
                row_get(row, "EquipmentItems"),
                row_get(row, "CompanyId"),
                raw_json,
            ),
        )
        return cur.fetchone()["id"]


def upsert_service_location(conn, row, source_system="skimmer"):
    source_location_id = row["id"]
    raw_json = json.dumps({k: row[k] for k in row.keys()})
    with conn.cursor() as cur:
        cur.execute(
            """
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
            RETURNING id
            """,
            (
                source_system,
                source_location_id,
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
                raw_json,
            ),
        )
        return cur.fetchone()["id"]


def import_pools(pg_conn, sqlite_conn, source_system="skimmer"):
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(
        "SELECT id, ServiceLocationId, Name, Gallons, BaselineFilterPressure, Notes, EquipmentItems, CompanyId FROM Pool"
    )
    rows = sqlite_cur.fetchall()
    imported = 0
    for row in rows:
        upsert_pool(pg_conn, row, source_system=source_system)
        imported += 1
    return imported


def import_service_locations(pg_conn, sqlite_conn, source_system="skimmer"):
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(
        "SELECT id, CustomerId, Address, City, State, Zip, LocationCode, Latitude, Longitude, MinutesAtStop, IsBadAddress, GateCode, DogsName, Notes, Rate, RateType, LaborCost, LaborCostType, Pools, RouteAssignments, RouteMoves, WorkOrderModels, RecurringWorkItems, CompanyId FROM ServiceLocation"
    )
    rows = sqlite_cur.fetchall()
    imported = 0
    for row in rows:
        upsert_service_location(pg_conn, row, source_system=source_system)
        imported += 1
    return imported


def import_entry_descriptions(pg_conn, sqlite_conn, source_system="skimmer"):
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(
        "SELECT id, Description, UnitOfMeasure, EntryType, ReadingType, DosageType, SelectedIndex, Sequence, Cost, Price, CanIncludeWithService, ColumnSequence, CompanyId FROM EntryDescription"
    )
    rows = sqlite_cur.fetchall()
    imported = 0
    for row in rows:
        upsert_entry_description(pg_conn, row, source_system=source_system)
        imported += 1
    return imported


def import_service_stop_entries(pg_conn, sqlite_conn, source_system="skimmer"):
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(
        "SELECT id, ServiceStopId, PoolId, EntryDescriptionId, EntryType, Value, ServiceDate, CompanyId, EntryDescriptionText, UnitOfMeasure, ReadingType, SelectedIndex, Sequence, ValueDisplay, WorkOrderId FROM ServiceStopEntry"
    )
    rows = sqlite_cur.fetchall()
    imported = 0
    for row in rows:
        upsert_service_stop_entry(pg_conn, row, source_system=source_system)
        imported += 1
    return imported


def import_customers(sqlite_conn, pg_conn, source_system="skimmer"):
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(
        "SELECT id, FirstName, LastName, CompanyName, PrimaryEmail, MobilePhone, MobilePhone2, BillingAddress, BillingCity, BillingState, BillingZip FROM Customer"
    )
    rows = sqlite_cur.fetchall()

    imported = 0
    identity_count = 0
    for row in rows:
        sk_customer_id = upsert_customer(pg_conn, row, source_system=source_system)
        identity_count += 1
        upsert_identity_map(pg_conn, sk_customer_id, row_get(row, "id"), "customer_id", source_system=source_system)
        upsert_identity_map(pg_conn, sk_customer_id, row_get(row, "PrimaryEmail"), "email", source_system=source_system)
        upsert_identity_map(pg_conn, sk_customer_id, row_get(row, "MobilePhone"), "mobile_phone", source_system=source_system)
        upsert_identity_map(pg_conn, sk_customer_id, row_get(row, "MobilePhone2"), "mobile_phone", source_system=source_system)
        imported += 1

    return imported, identity_count


def import_skimmer_data(sqlite_path, tables, source_system="skimmer"):
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
