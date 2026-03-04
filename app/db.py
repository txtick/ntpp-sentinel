import os
import sqlite3
import datetime as dt
from typing import Dict, List, Optional, Tuple

DB_PATH = os.getenv("DB_PATH", "/data/sentinel.db")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _col_exists(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)


def _ensure_columns(conn: sqlite3.Connection, table: str, cols: List[tuple]) -> None:
    for name, ddl in cols:
        if not _col_exists(conn, table, name):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    # Hot-path issue scans
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_status_type_due ON issues(status, issue_type, due_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_conversation_status ON issues(conversation_id, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_phone_status ON issues(phone, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_contact_status ON issues(contact_id, status)"
    )
    # Event retention / diagnostics scans
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_events_source_received ON raw_events(source, received_ts)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_raw_events_received ON raw_events(received_ts)"
    )


def ensure_schema() -> None:
    conn = db()

    # Existing column migrations on issues
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(issues)").fetchall()}

    # Newer columns that may not exist on older DBs
    if "contact_name" not in cols:
        conn.execute("ALTER TABLE issues ADD COLUMN contact_name TEXT")

    # Ensure v1 issue fields exist even if init_db didn't run on an older DB
    for col, ddl in [
        ("first_inbound_ts", "ALTER TABLE issues ADD COLUMN first_inbound_ts TEXT"),
        ("last_inbound_ts", "ALTER TABLE issues ADD COLUMN last_inbound_ts TEXT"),
        ("inbound_count", "ALTER TABLE issues ADD COLUMN inbound_count INTEGER DEFAULT 0"),
        ("outbound_count", "ALTER TABLE issues ADD COLUMN outbound_count INTEGER DEFAULT 0"),
        ("conversation_id", "ALTER TABLE issues ADD COLUMN conversation_id TEXT"),
        ("breach_notified_ts", "ALTER TABLE issues ADD COLUMN breach_notified_ts TEXT"),
    ]:
        if col not in cols:
            conn.execute(ddl)

    # Conversation-level state for internal-initiated threads
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_state (
            conversation_id TEXT PRIMARY KEY,
            last_internal_outbound_ts TEXT,
            last_internal_outbound_contact_id TEXT
        )
        """
    )

    # AI follow-up gate cache (optional)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_ai_gate (
            conversation_id TEXT PRIMARY KEY,
            last_msg_ts TEXT NOT NULL,
            needs_follow_up TEXT NOT NULL CHECK(needs_follow_up IN ('YES','NO')),
            confidence REAL NOT NULL,
            evidence_json TEXT NOT NULL,
            model TEXT NOT NULL,
            created_ts TEXT NOT NULL
        )
        """
    )

    _ensure_indexes(conn)
    conn.commit()
    conn.close()


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS raw_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts TEXT NOT NULL,
        source TEXT NOT NULL,
        payload TEXT NOT NULL
      )
    """
    )

    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS issues (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        issue_type TEXT NOT NULL,             -- 'SMS' | 'CALL'
        owner_id TEXT,
        contact_id TEXT,
        phone TEXT,
        created_ts TEXT NOT NULL,
        due_ts TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | RESOLVED | SPAM
        resolved_ts TEXT,
        meta TEXT
      )
    """
    )

    # Sentinel v1 issue fields
    _ensure_columns(
        conn,
        "issues",
        [
            ("first_inbound_ts", "TEXT"),
            ("last_inbound_ts", "TEXT"),
            ("inbound_count", "INTEGER DEFAULT 0"),
            ("outbound_count", "INTEGER DEFAULT 0"),
            ("conversation_id", "TEXT"),
            ("breach_notified_ts", "TEXT"),
        ],
    )

    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS spam_phones (
        phone TEXT PRIMARY KEY,
        created_ts TEXT NOT NULL
      )
    """
    )

    # For "resolved since last summary" dopamine
    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      )
    """
    )

    # AI follow-up gate cache (optional)
    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS conversation_ai_gate (
        conversation_id TEXT PRIMARY KEY,
        last_msg_ts TEXT NOT NULL,
        needs_follow_up TEXT NOT NULL CHECK(needs_follow_up IN ('YES','NO')),
        confidence REAL NOT NULL,
        evidence_json TEXT NOT NULL,
        model TEXT NOT NULL,
        created_ts TEXT NOT NULL
      )
    """
    )

    # Conversation-level state for internal-initiated threads
    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS conversation_state (
        conversation_id TEXT PRIMARY KEY,
        last_internal_outbound_ts TEXT,
        last_internal_outbound_contact_id TEXT
      )
    """
    )

    _ensure_indexes(conn)
    conn.commit()
    conn.close()


def purge_raw_events(retention_days: int, source: Optional[str] = None, dry_run: bool = True) -> Dict[str, int]:
    """
    Deletes raw events older than retention_days (UTC), optionally scoped by source.
    Returns {'eligible': int, 'deleted': int}.
    """
    days = max(1, int(retention_days))
    cutoff = (dt.datetime.utcnow() - dt.timedelta(days=days)).isoformat()

    conn = db()
    where = "received_ts < ?"
    params: List[object] = [cutoff]
    if source:
        where += " AND source = ?"
        params.append(source)

    eligible = int(
        conn.execute(f"SELECT COUNT(*) AS n FROM raw_events WHERE {where}", params).fetchone()["n"]  # nosec B608
    )

    deleted = 0
    if not dry_run and eligible > 0:
        cur = conn.execute(f"DELETE FROM raw_events WHERE {where}", params)  # nosec B608
        conn.commit()
        deleted = int(cur.rowcount if cur.rowcount is not None else 0)

    conn.close()
    return {"eligible": eligible, "deleted": deleted}
