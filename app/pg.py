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
        conn.commit()
