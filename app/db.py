import os
import sqlite3
import datetime as dt
from typing import Dict, List, Optional, Tuple

DB_PATH = os.getenv("DB_PATH", "/data/sentinel.db")
SQLITE_TIMEOUT_SECONDS = float(os.getenv("SQLITE_TIMEOUT_SECONDS", "30"))
SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
ISSUE_DISPLAY_ID_MAX = max(9, int(os.getenv("ISSUE_DISPLAY_ID_MAX", "99")))


def db() -> sqlite3.Connection:
    # timeout/busy_timeout: wait before failing when another writer holds the lock
    # WAL mode: allows concurrent readers alongside a writer, reducing contention
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
    # Resolved-issues range scans (used by fetch_resolved_since in summary jobs)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_issues_status_resolved_ts ON issues(status, resolved_ts)"
    )
    if _col_exists(conn, "issues", "display_id"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_status_display_id ON issues(status, display_id)"
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
        ("display_id", "ALTER TABLE issues ADD COLUMN display_id INTEGER"),
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

    # Route rollover sessions (transient operational state; SQLite only)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rollover_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id TEXT NOT NULL,
            conversation_id TEXT,
            state TEXT NOT NULL,
            route_stops_json TEXT NOT NULL,
            selected_indices TEXT,
            created_ts REAL NOT NULL,
            updated_ts REAL NOT NULL,
            completed_ts REAL
        )
        """
    )

    _ensure_indexes(conn)
    _backfill_issue_display_ids(conn)
    conn.commit()
    conn.close()


def allocate_issue_display_id(conn: sqlite3.Connection, max_display_id: int = ISSUE_DISPLAY_ID_MAX) -> int:
    rows = conn.execute(
        """
        SELECT display_id
        FROM issues
        WHERE status IN ('OPEN','PENDING')
          AND display_id IS NOT NULL
        """
    ).fetchall()
    used = {int(r["display_id"]) for r in rows if r["display_id"] is not None}

    for candidate in range(1, max_display_id + 1):
        if candidate not in used:
            return candidate

    candidate = max_display_id + 1
    while candidate in used:
        candidate += 1
    return candidate


def _backfill_issue_display_ids(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id
        FROM issues
        WHERE status IN ('OPEN','PENDING')
          AND display_id IS NULL
        ORDER BY due_ts ASC, id ASC
        """
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE issues SET display_id=? WHERE id=?",
            (allocate_issue_display_id(conn), row["id"]),
        )


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

    # Route rollover sessions (transient operational state; SQLite only)
    cur.execute(
        """
      CREATE TABLE IF NOT EXISTS rollover_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id TEXT NOT NULL,
        conversation_id TEXT,
        state TEXT NOT NULL,
        route_stops_json TEXT NOT NULL,
        selected_indices TEXT,
        created_ts REAL NOT NULL,
        updated_ts REAL NOT NULL,
        completed_ts REAL
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
