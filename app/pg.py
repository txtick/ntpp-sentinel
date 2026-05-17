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
                    is_inactive BOOLEAN,
                    is_lead BOOLEAN,
                    customer_status TEXT,
                    ghl_contact_id TEXT,
                    ghl_last_sync_ts TIMESTAMPTZ,
                    ghl_last_sync_error TEXT,
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
            cur.execute("ALTER TABLE sk_customer ADD COLUMN IF NOT EXISTS is_inactive BOOLEAN")
            cur.execute("ALTER TABLE sk_customer ADD COLUMN IF NOT EXISTS is_lead BOOLEAN")
            cur.execute("ALTER TABLE sk_customer ADD COLUMN IF NOT EXISTS customer_status TEXT")
            cur.execute("ALTER TABLE sk_customer ADD COLUMN IF NOT EXISTS ghl_contact_id TEXT")
            cur.execute("ALTER TABLE sk_customer ADD COLUMN IF NOT EXISTS ghl_last_sync_ts TIMESTAMPTZ")
            cur.execute("ALTER TABLE sk_customer ADD COLUMN IF NOT EXISTS ghl_last_sync_error TEXT")
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_customer_status
                ON sk_customer(source_system, customer_status)
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
                CREATE TABLE IF NOT EXISTS sk_account (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_account_id TEXT NOT NULL,
                    username TEXT,
                    email TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    role_type TEXT,
                    is_active BOOLEAN,
                    mobile_phone TEXT,
                    address TEXT,
                    city TEXT,
                    state TEXT,
                    zip TEXT,
                    company_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_account_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_route_assignment (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_route_assignment_id TEXT NOT NULL,
                    source_service_location_id TEXT,
                    source_account_id TEXT,
                    day_of_week TEXT,
                    frequency TEXT,
                    start_date TIMESTAMPTZ,
                    end_date TIMESTAMPTZ,
                    sequence INTEGER,
                    company_id TEXT,
                    status TEXT,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_route_assignment_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_route_assignment_location
                ON sk_route_assignment(source_system, source_service_location_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_route_assignment_account
                ON sk_route_assignment(source_system, source_account_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_route_stop (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_route_stop_id TEXT NOT NULL,
                    source_account_id TEXT,
                    source_service_location_id TEXT,
                    source_route_assignment_id TEXT,
                    service_date TIMESTAMPTZ,
                    start_time TIMESTAMPTZ,
                    complete_time TIMESTAMPTZ,
                    is_skipped BOOLEAN NOT NULL DEFAULT FALSE,
                    skipped_stop_reason_id TEXT,
                    is_off_biweekly BOOLEAN NOT NULL DEFAULT FALSE,
                    sequence INTEGER,
                    minutes_at_stop INTEGER,
                    source_route_move_id TEXT,
                    email_status TEXT,
                    email_sent_date TIMESTAMPTZ,
                    company_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_route_stop_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_route_stop_service_date
                ON sk_route_stop(source_system, service_date DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_route_stop_account
                ON sk_route_stop(source_system, source_account_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_route_stop_location
                ON sk_route_stop(source_system, source_service_location_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_work_order_type (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_work_order_type_id TEXT NOT NULL,
                    description TEXT,
                    default_work_needed TEXT,
                    default_work_performed TEXT,
                    default_minutes INTEGER,
                    default_labor_cost NUMERIC,
                    default_price NUMERIC,
                    default_email_subject TEXT,
                    default_email_header TEXT,
                    default_email_message TEXT,
                    company_id TEXT,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_work_order_type_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_work_order_type_description
                ON sk_work_order_type(source_system, description)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sk_work_order (
                    id BIGSERIAL PRIMARY KEY,
                    source_system TEXT NOT NULL DEFAULT 'skimmer',
                    source_work_order_id TEXT NOT NULL,
                    source_work_order_type_id TEXT,
                    source_service_location_id TEXT,
                    work_needed TEXT,
                    work_performed TEXT,
                    source_added_by_account_id TEXT,
                    added_on_date TIMESTAMPTZ,
                    source_account_id TEXT,
                    service_date TIMESTAMPTZ,
                    scheduled_time TEXT,
                    start_time TIMESTAMPTZ,
                    complete_time TIMESTAMPTZ,
                    route_sequence INTEGER,
                    estimated_minutes INTEGER,
                    labor_cost NUMERIC,
                    price NUMERIC,
                    is_invoiced BOOLEAN,
                    sync_date TIMESTAMPTZ,
                    email_header TEXT,
                    email_message TEXT,
                    email_status TEXT,
                    email_sent_date TIMESTAMPTZ,
                    company_id TEXT,
                    notes TEXT,
                    note_is_alert BOOLEAN,
                    note_is_handled BOOLEAN,
                    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
                    raw_json JSONB,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (source_system, source_work_order_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_work_order_location_date
                ON sk_work_order(source_system, source_service_location_id, service_date DESC)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sk_work_order_type_ref
                ON sk_work_order(source_system, source_work_order_type_id)
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


def ensure_route_sandbox_schema() -> None:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS technician_route_profiles (
                    id BIGSERIAL PRIMARY KEY,
                    technician_id TEXT NOT NULL,
                    display_name TEXT,
                    home_address TEXT,
                    home_latitude NUMERIC,
                    home_longitude NUMERIC,
                    default_start_location_type TEXT NOT NULL DEFAULT 'first_stop',
                    default_end_location_type TEXT NOT NULL DEFAULT 'last_stop',
                    custom_start_address TEXT,
                    custom_start_latitude NUMERIC,
                    custom_start_longitude NUMERIC,
                    custom_end_address TEXT,
                    custom_end_latitude NUMERIC,
                    custom_end_longitude NUMERIC,
                    include_start_drive_in_metrics BOOLEAN NOT NULL DEFAULT FALSE,
                    include_end_drive_in_metrics BOOLEAN NOT NULL DEFAULT FALSE,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    notes TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (technician_id)
                )
                """
            )
            cur.execute(
                "ALTER TABLE technician_route_profiles ALTER COLUMN include_start_drive_in_metrics SET DEFAULT FALSE"
            )
            cur.execute(
                "ALTER TABLE technician_route_profiles ALTER COLUMN include_end_drive_in_metrics SET DEFAULT FALSE"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_scenarios (
                    id BIGSERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    source_snapshot_date DATE,
                    created_by TEXT,
                    notes TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                UPDATE route_scenarios
                SET status = 'manually_completed'
                WHERE status = 'pushed'
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_scenario_assignments (
                    id BIGSERIAL PRIMARY KEY,
                    scenario_id BIGINT NOT NULL REFERENCES route_scenarios(id) ON DELETE CASCADE,
                    source_route_assignment_id TEXT,
                    source_service_location_id TEXT NOT NULL,
                    source_customer_id TEXT,
                    source_account_id TEXT NOT NULL,
                    day_of_week TEXT NOT NULL,
                    stop_order INTEGER,
                    frequency TEXT,
                    estimated_duration_minutes INTEGER,
                    latitude NUMERIC,
                    longitude NUMERIC,
                    address TEXT,
                    city TEXT,
                    customer_name TEXT,
                    tech_name TEXT,
                    rate_type TEXT,
                    notes TEXT,
                    is_changed_from_current BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_scenario_assignments_scenario
                ON route_scenario_assignments(scenario_id, day_of_week, source_account_id, stop_order)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_scenario_assignments_pool
                ON route_scenario_assignments(source_service_location_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_scenario_assignments_technician
                ON route_scenario_assignments(source_account_id)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_scenario_assignments_service_day
                ON route_scenario_assignments(day_of_week)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_scenario_assignments_scenario_tech_day
                ON route_scenario_assignments(scenario_id, source_account_id, day_of_week)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_change_plans (
                    id BIGSERIAL PRIMARY KEY,
                    scenario_id BIGINT NOT NULL REFERENCES route_scenarios(id) ON DELETE CASCADE,
                    status TEXT NOT NULL DEFAULT 'generated',
                    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    scenario_updated_at TIMESTAMPTZ,
                    approved_by TEXT,
                    approved_at TIMESTAMPTZ,
                    manually_completed_at TIMESTAMPTZ,
                    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    error_json JSONB,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute("ALTER TABLE route_change_plans ADD COLUMN IF NOT EXISTS scenario_updated_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE route_change_plans ADD COLUMN IF NOT EXISTS manually_completed_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE route_change_plans ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            cur.execute(
                """
                UPDATE route_change_plans
                SET status = 'manually_completed'
                WHERE status = 'pushed'
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_change_plans_scenario
                ON route_change_plans(scenario_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_change_plan_items (
                    id BIGSERIAL PRIMARY KEY,
                    change_plan_id BIGINT NOT NULL REFERENCES route_change_plans(id) ON DELETE CASCADE,
                    change_type TEXT NOT NULL,
                    source_service_location_id TEXT,
                    customer_name TEXT,
                    address TEXT,
                    source_account_id_current TEXT,
                    day_of_week_current TEXT,
                    stop_order_current INTEGER,
                    source_account_id_proposed TEXT,
                    day_of_week_proposed TEXT,
                    stop_order_proposed INTEGER,
                    skimmer_route_assignment_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_change_plan_items_plan
                ON route_change_plan_items(change_plan_id)
                """
            )
        conn.commit()


def ensure_route_maps_schema() -> None:
    """Create distance cache and route estimate tables for Google Maps integration."""
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_distance_cache (
                    id BIGSERIAL PRIMARY KEY,
                    origin_hash TEXT NOT NULL,
                    destination_hash TEXT NOT NULL,
                    origin_latitude NUMERIC NOT NULL,
                    origin_longitude NUMERIC NOT NULL,
                    destination_latitude NUMERIC NOT NULL,
                    destination_longitude NUMERIC NOT NULL,
                    travel_mode TEXT NOT NULL DEFAULT 'DRIVE',
                    distance_meters INTEGER NOT NULL,
                    duration_seconds INTEGER NOT NULL,
                    duration_in_traffic_seconds INTEGER,
                    polyline TEXT,
                    provider TEXT NOT NULL DEFAULT 'google_maps',
                    calculated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (origin_hash, destination_hash, travel_mode)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_distance_cache_lookup
                ON route_distance_cache(origin_hash, destination_hash, travel_mode)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_estimate_runs (
                    id BIGSERIAL PRIMARY KEY,
                    scenario_id BIGINT REFERENCES route_scenarios(id) ON DELETE SET NULL,
                    source_type TEXT NOT NULL DEFAULT 'scenario',
                    technician_id TEXT,
                    service_day TEXT,
                    provider TEXT NOT NULL DEFAULT 'google_maps',
                    status TEXT NOT NULL DEFAULT 'pending',
                    total_distance_meters INTEGER,
                    total_duration_seconds INTEGER,
                    customer_to_customer_distance_meters INTEGER,
                    customer_to_customer_duration_seconds INTEGER,
                    start_to_first_distance_meters INTEGER,
                    start_to_first_duration_seconds INTEGER,
                    last_to_end_distance_meters INTEGER,
                    last_to_end_duration_seconds INTEGER,
                    service_duration_seconds INTEGER,
                    total_work_duration_seconds INTEGER,
                    stop_count INTEGER,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    cache_hit_count INTEGER NOT NULL DEFAULT 0,
                    is_stale BOOLEAN NOT NULL DEFAULT FALSE,
                    error_message TEXT,
                    warnings JSONB,
                    polyline TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_estimate_runs_scenario
                ON route_estimate_runs(scenario_id, technician_id, service_day)
                """
            )
            cur.execute("ALTER TABLE route_estimate_runs ADD COLUMN IF NOT EXISTS is_stale BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_google_usage_daily (
                    usage_date DATE PRIMARY KEY,
                    request_count INTEGER NOT NULL DEFAULT 0,
                    matrix_element_count INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS route_estimate_segments (
                    id BIGSERIAL PRIMARY KEY,
                    estimate_run_id BIGINT NOT NULL REFERENCES route_estimate_runs(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    from_type TEXT NOT NULL,
                    from_pool_id TEXT,
                    from_latitude NUMERIC,
                    from_longitude NUMERIC,
                    to_type TEXT NOT NULL,
                    to_pool_id TEXT,
                    to_latitude NUMERIC,
                    to_longitude NUMERIC,
                    distance_meters INTEGER NOT NULL DEFAULT 0,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    duration_in_traffic_seconds INTEGER,
                    polyline TEXT,
                    provider TEXT NOT NULL DEFAULT 'google_maps',
                    cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
                    error_message TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_route_estimate_segments_run
                ON route_estimate_segments(estimate_run_id, sequence)
                """
            )
        conn.commit()


def get_pools_with_service_location(source_system: str = "skimmer") -> list[dict]:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM sk_pool_with_service_location WHERE source_system = %s",
                (source_system,),
            )
            return cur.fetchall()
