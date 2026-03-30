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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_entry_description (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_entry_description_id TEXT NOT NULL,
                    description TEXT,
                    unit_of_measure TEXT,
                    entry_type TEXT,
                    reading_type TEXT,
                    dosage_type TEXT,
                    selected_index INTEGER,
                    sequence INTEGER,
                    cost NUMERIC,
                    price NUMERIC,
                    can_include_with_service BOOLEAN,
                    column_sequence INTEGER,
                    company_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_entry_description_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_pool (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_pool_id TEXT NOT NULL,
                    source_service_location_id TEXT,
                    name TEXT,
                    gallons INTEGER,
                    baseline_filter_pressure NUMERIC,
                    notes TEXT,
                    equipment_items JSONB,
                    company_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_pool_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_service_location (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_location_id TEXT NOT NULL,
                    source_customer_id TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    location_code TEXT,
                    latitude NUMERIC,
                    longitude NUMERIC,
                    minutes_at_stop INTEGER,
                    is_bad_address BOOLEAN,
                    gate_code TEXT,
                    dogs_name TEXT,
                    notes TEXT,
                    rate NUMERIC,
                    rate_type TEXT,
                    labor_cost NUMERIC,
                    labor_cost_type TEXT,
                    pools JSONB,
                    route_assignments JSONB,
                    route_moves JSONB,
                    work_order_models JSONB,
                    recurring_work_items JSONB,
                    company_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_location_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_pool_source_service_location
                ON sk_pool(source_system, source_service_location_id)
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE VIEW sk_pool_with_service_location AS
                SELECT
                    p.*,
                    s.id AS service_location_id,
                    s.source_location_id AS service_location_source_location_id,
                    s.source_customer_id AS service_location_source_customer_id,
                    s.address AS service_location_address,
                    s.city AS service_location_city,
                    s.state AS service_location_state,
                    s.zip AS service_location_zip,
                    s.location_code AS service_location_code,
                    s.latitude AS service_location_latitude,
                    s.longitude AS service_location_longitude,
                    s.minutes_at_stop AS service_location_minutes_at_stop,
                    s.is_bad_address AS service_location_is_bad_address,
                    s.gate_code AS service_location_gate_code,
                    s.dogs_name AS service_location_dogs_name,
                    s.notes AS service_location_notes,
                    s.rate AS service_location_rate,
                    s.rate_type AS service_location_rate_type,
                    s.labor_cost AS service_location_labor_cost,
                    s.labor_cost_type AS service_location_labor_cost_type,
                    s.pools AS service_location_pools,
                    s.route_assignments AS service_location_route_assignments,
                    s.route_moves AS service_location_route_moves,
                    s.work_order_models AS service_location_work_order_models,
                    s.recurring_work_items AS service_location_recurring_work_items,
                    s.company_id AS service_location_company_id,
                    s.raw_json AS service_location_raw_json,
                    s.created_at AS service_location_created_at,
                    s.updated_at AS service_location_updated_at
                FROM sk_pool p
                LEFT JOIN sk_service_location s ON
                    p.source_system = s.source_system
                    AND p.source_service_location_id = s.source_location_id
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_service_stop_entry (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_entry_id TEXT NOT NULL,
                    source_service_stop_id TEXT,
                    source_pool_id TEXT,
                    source_entry_description_id TEXT,
                    entry_type TEXT,
                    value NUMERIC,
                    service_date TIMESTAMPTZ,
                    company_id TEXT,
                    entry_description_text TEXT,
                    unit_of_measure TEXT,
                    reading_type TEXT,
                    selected_index INTEGER,
                    sequence INTEGER,
                    value_display TEXT,
                    work_order_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_entry_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_service_stop_entry_source_pool
                ON sk_service_stop_entry(source_system, source_pool_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_service_stop_entry_service_date
                ON sk_service_stop_entry(source_system, service_date)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_service_stop_entry_description
                ON sk_service_stop_entry(source_system, source_entry_description_id)
                """
            )
            cur.execute(
                """
                CREATE OR REPLACE VIEW sk_service_stop_entry_with_location AS
                SELECT
                    e.*,
                    p.source_service_location_id,
                    s.source_customer_id AS service_location_source_customer_id,
                    d.description AS entry_description_description,
                    d.unit_of_measure AS entry_description_unit_of_measure,
                    d.entry_type AS entry_description_type,
                    d.reading_type AS entry_description_reading_type
                FROM sk_service_stop_entry e
                LEFT JOIN sk_pool p ON e.source_system = p.source_system AND e.source_pool_id = p.source_pool_id
                LEFT JOIN sk_service_location s ON p.source_system = s.source_system AND p.source_service_location_id = s.source_location_id
                LEFT JOIN sk_entry_description d ON e.source_system = d.source_system AND e.source_entry_description_id = d.source_entry_description_id
                """
            )
        conn.commit()


def get_service_stop_entries_with_location(source_system: str = "skimmer") -> list[dict]:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sk_service_stop_entry_with_location WHERE source_system = %s",
                (source_system,),
            )
            return cur.fetchall()


def get_pools_with_service_location(source_system: str = "skimmer") -> list[dict]:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sk_pool_with_service_location WHERE source_system = %s",
                (source_system,),
            )
            return cur.fetchall()
