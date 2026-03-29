import os

import psycopg
from psycopg.rows import dict_row

DATABASE_URL = os.getenv("DATABASE_URL")


def pg() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def pg_healthcheck() -> dict:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() AS now")
            return cur.fetchone()


def ensure_pg_schema() -> None:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS skimmer_import_runs (
                    id BIGSERIAL PRIMARY KEY,
                    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source_filename TEXT,
                    db_path TEXT,
                    success BOOLEAN NOT NULL,
                    error_message TEXT,
                    table_counts_json JSONB,
                    snapshot_date TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_customer (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_customer_id TEXT NOT NULL,
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
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_customer_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_identity_map (
                    id BIGSERIAL PRIMARY KEY,
                    sk_customer_id BIGINT NOT NULL REFERENCES sk_customer(id) ON DELETE CASCADE,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_id TEXT NOT NULL,
                    identity_type TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_id, identity_type)
                )
                """
            )
        conn.commit()
