#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

from app.pg import pg


def open_skimmer_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


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
                row.get("FirstName"),
                row.get("LastName"),
                row.get("CompanyName"),
                row.get("PrimaryEmail"),
                row.get("MobilePhone"),
                row.get("MobilePhone2"),
                row.get("BillingAddress"),
                row.get("BillingCity"),
                row.get("BillingState"),
                row.get("BillingZip"),
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


def import_customers(sqlite_path, source_system="skimmer"):
    sqlite_conn = open_skimmer_sqlite(sqlite_path)
    sqlite_cur = sqlite_conn.cursor()
    sqlite_cur.execute(
        "SELECT id, FirstName, LastName, CompanyName, PrimaryEmail, MobilePhone, MobilePhone2, BillingAddress, BillingCity, BillingState, BillingZip FROM Customer"
    )
    rows = sqlite_cur.fetchall()

    with pg() as pg_conn:
        run_id = insert_import_run(pg_conn, os.path.basename(sqlite_path), sqlite_path)
        imported = 0
        identity_count = 0
        try:
            for row in rows:
                sk_customer_id = upsert_customer(pg_conn, row, source_system=source_system)
                identity_count += 1
                upsert_identity_map(pg_conn, sk_customer_id, row.get("id"), "customer_id", source_system=source_system)
                upsert_identity_map(pg_conn, sk_customer_id, row.get("PrimaryEmail"), "email", source_system=source_system)
                upsert_identity_map(pg_conn, sk_customer_id, row.get("MobilePhone"), "mobile_phone", source_system=source_system)
                upsert_identity_map(pg_conn, sk_customer_id, row.get("MobilePhone2"), "mobile_phone", source_system=source_system)
                imported += 1
            update_import_run(
                pg_conn,
                run_id,
                True,
                error_message=None,
                table_counts={
                    "customers_imported": imported,
                    "identity_records": identity_count,
                },
            )
            pg_conn.commit()
        except Exception as exc:
            pg_conn.rollback()
            update_import_run(pg_conn, run_id, False, error_message=str(exc), table_counts={"customers_imported": imported})
            pg_conn.commit()
            raise
    sqlite_conn.close()
    return imported


def parse_args():
    parser = argparse.ArgumentParser(description="Import Customer data from a Skimmer SQLite export into Postgres.")
    parser.add_argument("--sqlite", default=os.getenv("SKIMMER_DB_PATH"), help="Path to the Skimmer SQLite export file.")
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

    imported = import_customers(args.sqlite, source_system=args.source_system)
    print(f"Imported {imported} customers from {args.sqlite}")


if __name__ == "__main__":
    main()
