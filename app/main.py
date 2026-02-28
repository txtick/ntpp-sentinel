from fastapi import FastAPI, Request, HTTPException # type: ignore
import os, json, sqlite3, datetime as dt
from typing import Any, Dict, Optional, List, Tuple
import httpx # type: ignore
import re
from zoneinfo import ZoneInfo

# ==========================
# Config
# ==========================
DB_PATH = os.getenv("DB_PATH", "/data/sentinel.db")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
TZ_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))

GHL_APP_BASE = os.getenv("GHL_APP_BASE", "https://app.gohighlevel.com")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")

def _parse_hhmm(value: str, fallback_hour: int, fallback_minute: int) -> Tuple[int, int]:
    s = (value or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if not m:
        return fallback_hour, fallback_minute
    h = int(m.group(1))
    mm = int(m.group(2))
    if h < 0 or h > 23 or mm < 0 or mm > 59:
        return fallback_hour, fallback_minute
    return h, mm

_bh_start_h, _bh_start_m = _parse_hhmm(os.getenv("BUSINESS_HOURS_START", "08:00"), 8, 0)
_bh_end_h, _bh_end_m = _parse_hhmm(os.getenv("BUSINESS_HOURS_END", "17:00"), 17, 0)
_bh_start_total = (_bh_start_h * 60) + _bh_start_m
_bh_end_total = (_bh_end_h * 60) + _bh_end_m
if _bh_end_total <= _bh_start_total:
    _bh_start_h, _bh_start_m = 8, 0
    _bh_end_h, _bh_end_m = 17, 0

# GoHighLevel / LeadConnector API
GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")
GHL_TOKEN = os.getenv("GHL_TOKEN", "")  # Private Integration token (Bearer)
GHL_VERSION = os.getenv("GHL_VERSION", "2021-07-28")
# OpenAI (AI follow-up gate; optional)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
AI_GATE_ENABLED = os.getenv("AI_GATE_ENABLED", "0").lower() in ("1","true","yes","on")
AI_GATE_MODEL = os.getenv("AI_GATE_MODEL", "gpt-5-mini")
AI_GATE_SUPPRESS_NO_CONFIDENCE = float(os.getenv("AI_GATE_SUPPRESS_NO_CONFIDENCE", "0.90"))
AI_GATE_MAX_MESSAGES = int(os.getenv("AI_GATE_MAX_MESSAGES", "10"))
AI_GATE_GAP_HOURS = float(os.getenv("AI_GATE_GAP_HOURS", "4"))
AI_GATE_MAX_ISSUES_PER_RUN = int(os.getenv("AI_GATE_MAX_ISSUES_PER_RUN", "20"))
AI_GATE_RUN_BUDGET_SECONDS = float(os.getenv("AI_GATE_RUN_BUDGET_SECONDS", "20"))
AI_GATE_TIMEOUT_SECONDS = float(os.getenv("AI_GATE_TIMEOUT_SECONDS", "4"))
AI_GATE_REDACT_PII = os.getenv("AI_GATE_REDACT_PII", "1").lower() in ("1","true","yes","on")

# Summary recipients (managers only, v1)
MANAGER_CONTACT_IDS = [
    s.strip() for s in (os.getenv("MANAGER_CONTACT_IDS", "")).split(",") if s.strip()
]

# Internal manager contact whitelist and reply grace window
INTERNAL_CONTACT_IDS = set(
    x.strip()
    for x in (os.getenv("INTERNAL_CONTACT_IDS", "")).split(",")
    if x.strip()
)
INTERNAL_REPLY_GRACE_HOURS = int(os.getenv("INTERNAL_REPLY_GRACE_HOURS", "12"))

# Ack close-out suppression (customer 'thanks/👍/fixed it' after staff reply)
ACK_CLOSE_ENABLED = os.getenv("ACK_CLOSE_ENABLED", "1").lower() in ("1","true","yes","on")
ACK_CLOSE_WINDOW_MODE = os.getenv("ACK_CLOSE_WINDOW_MODE", "eod").lower()  # 'eod' | 'hours'
ACK_CLOSE_WINDOW_HOURS = float(os.getenv("ACK_CLOSE_WINDOW_HOURS", str(INTERNAL_REPLY_GRACE_HOURS)))
ACK_CLOSE_MAX_LEN = int(os.getenv("ACK_CLOSE_MAX_LEN", "80"))

# Limits to keep SMS short and low-noise
SUMMARY_MAX_ITEMS_PER_SECTION = int(os.getenv("SUMMARY_MAX_ITEMS_PER_SECTION", "8"))
RESOLVED_SINCE_MAX_ITEMS = 5
FLOW_LOG_ENABLED = os.getenv("FLOW_LOG_ENABLED", "1").lower() in ("1", "true", "yes", "on")

# SLA for customer SMS and CALL response before it is considered an issue (hours)
SMS_SLA_HOURS = float(os.getenv("SMS_SLA_HOURS", "2"))
CALL_SLA_HOURS = float(os.getenv("CALL_SLA_HOURS", "2"))

app = FastAPI()


# ==========================
# DB helpers + migrations
# ==========================
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
    conn.commit()
    conn.close()

def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
      CREATE TABLE IF NOT EXISTS raw_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_ts TEXT NOT NULL,
        source TEXT NOT NULL,
        payload TEXT NOT NULL
      )
    """)

    cur.execute("""
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
    """)

    # Sentinel v1 issue fields
    _ensure_columns(conn, "issues", [
        ("first_inbound_ts", "TEXT"),
        ("last_inbound_ts", "TEXT"),
        ("inbound_count", "INTEGER DEFAULT 0"),
        ("outbound_count", "INTEGER DEFAULT 0"),
        ("conversation_id", "TEXT"),
        ("breach_notified_ts", "TEXT"),
    ])

    cur.execute("""
      CREATE TABLE IF NOT EXISTS spam_phones (
        phone TEXT PRIMARY KEY,
        created_ts TEXT NOT NULL
      )
    """)

    # For "resolved since last summary" dopamine
    cur.execute("""
      CREATE TABLE IF NOT EXISTS kv_store (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      )
    """)


    # AI follow-up gate cache (optional)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS conversation_ai_gate (
        conversation_id TEXT PRIMARY KEY,
        last_msg_ts TEXT NOT NULL,
        needs_follow_up TEXT NOT NULL CHECK(needs_follow_up IN ('YES','NO')),
        confidence REAL NOT NULL,
        evidence_json TEXT NOT NULL,
        model TEXT NOT NULL,
        created_ts TEXT NOT NULL
      )
    """)

    conn.commit()
    conn.close()


def set_last_internal_outbound(
    conversation_id: str, ts_iso: str, internal_contact_id: Optional[str]
) -> None:
    conn = db()
    conn.execute(
        """
      INSERT INTO conversation_state (conversation_id, last_internal_outbound_ts, last_internal_outbound_contact_id)
      VALUES (?, ?, ?)
      ON CONFLICT(conversation_id) DO UPDATE SET
        last_internal_outbound_ts=excluded.last_internal_outbound_ts,
        last_internal_outbound_contact_id=excluded.last_internal_outbound_contact_id
    """,
        (conversation_id, ts_iso, internal_contact_id),
    )
    conn.commit()
    conn.close()


def get_last_internal_outbound(conversation_id: str) -> Optional[str]:
    conn = db()
    row = conn.execute(
        """
      SELECT last_internal_outbound_ts FROM conversation_state WHERE conversation_id=?
    """,
        (conversation_id,),
    ).fetchone()
    conn.close()
    return row["last_internal_outbound_ts"] if row else None
# ==========================
# Ack close-out helpers
# ==========================
_ACK_EMOJI_RE = re.compile(r"^[\s\W_]*(?:👍|👌|✅|🙏|🙂|😀|😄|😊|🙌|🤝|🎉|🥰|😅|😂|😉)+[\s\W_]*$", re.UNICODE)

_ACK_PHRASES = {
    "thanks", "thank you", "thx", "ty",
    "ok", "okay", "k", "kk",
    "cool", "cool thanks", "sounds good", "sg", "got it", "perfect",
    "great", "awesome", "cool beans", "nice",
    "all good", "all good now", "we're good", "we are good",
    "no worries", "no problem",
    "done", "resolved", "handled", "taken care of", "took care of it",
    "fixed", "fixed it", "i fixed it", "we fixed it", "got it fixed",
    "cancel", "cancelled", "nevermind", "never mind",
}

_ACK_REACTION_PREFIXES = (
    "liked ",
    "loved ",
    "disliked ",
    "laughed at ",
    "emphasized ",
    "questioned ",
)

def _normalize_text_for_match(s: str) -> str:
    t = (s or "").strip().lower()
    t = re.sub(r"[\r\n\t]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # strip punctuation for phrase matching
    t2 = re.sub(r"[^\w\s]", "", t).strip()
    return t2

def _is_ack_closeout(text: Optional[str]) -> bool:
    if not text:
        return False
    raw = text.strip()
    if not raw:
        return False
    if len(raw) > ACK_CLOSE_MAX_LEN:
        return False
    if _ACK_EMOJI_RE.match(raw):
        return True
    t = _normalize_text_for_match(raw)
    if not t:
        return False
    if t in _ACK_PHRASES:
        return True
    # iMessage-style reaction/tapback text (e.g., "Liked “Sounds good”")
    if any(t.startswith(p) for p in _ACK_REACTION_PREFIXES):
        return True
    # common short variants
    if t.startswith("fixed it") or t.endswith("fixed it"):
        return True
    if "got it fixed" in t or "took care of" in t or "taken care of" in t:
        return True
    return False

def _next_business_day(d: dt.datetime) -> dt.datetime:
    cur = d
    # roll to next weekday if weekend
    while cur.weekday() >= 5:
        cur = cur + dt.timedelta(days=1)
    return cur

def _business_day_end_for(ts_local: dt.datetime) -> dt.datetime:
    """Returns the business-day end boundary (configured end time) for the day of ts_local.
    If ts_local is after today's business end, returns next business day's end.
    """
    if ts_local.tzinfo is None:
        ts_local = ts_local.replace(tzinfo=ZoneInfo(TZ_NAME))
    # normalize to local tz
    ts_local = ts_local.astimezone(ZoneInfo(TZ_NAME))
    end_today = ts_local.replace(hour=_bh_end_h, minute=_bh_end_m, second=0, microsecond=0)
    base_day = ts_local
    if ts_local > end_today:
        base_day = (ts_local + dt.timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    base_day = _next_business_day(base_day)
    end_day = base_day.replace(hour=_bh_end_h, minute=_bh_end_m, second=0, microsecond=0)
    return end_day


@app.on_event("startup")
def _startup():
    os.makedirs("/data", exist_ok=True)
    init_db()
    ensure_schema()

@app.get("/health")
def health():
    return {"ok": True}


# ==========================
# Auth helper (shared)
# ==========================
def _auth_or_401(request: Request) -> None:
    secret = request.headers.get("X-NTPP-Secret") or request.query_params.get("secret")
    if not secret or secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ==========================
# Time / SLA helpers
# ==========================
def _now_local() -> dt.datetime:
    return dt.datetime.now(tz=ZoneInfo(TZ_NAME))


def _parse_iso_dt(value) -> Optional[dt.datetime]:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        try:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

def _parse_ghl_date(value) -> Optional[dt.datetime]:
    """Parse a GHL/LeadConnector timestamp (e.g. '2026-02-26T14:00:02.992Z') to a datetime."""
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None

def _is_business_time(ts: dt.datetime) -> bool:
    # Mon-Fri in configured business-hour window.
    if ts.weekday() >= 5:
        return False
    start = ts.replace(hour=_bh_start_h, minute=_bh_start_m, second=0, microsecond=0)
    end = ts.replace(hour=_bh_end_h, minute=_bh_end_m, second=0, microsecond=0)
    return start <= ts <= end

def _roll_to_next_business_open(ts: dt.datetime) -> dt.datetime:
    cur = ts
    while True:
        if cur.weekday() >= 5:
            days_ahead = 7 - cur.weekday()
            cur = (cur + dt.timedelta(days=days_ahead)).replace(
                hour=_bh_start_h, minute=_bh_start_m, second=0, microsecond=0
            )
            continue
        cur_mins = (cur.hour * 60) + cur.minute
        if cur_mins < _bh_start_total:
            return cur.replace(hour=_bh_start_h, minute=_bh_start_m, second=0, microsecond=0)
        if cur_mins >= _bh_end_total:
            cur = (cur + dt.timedelta(days=1)).replace(
                hour=_bh_start_h, minute=_bh_start_m, second=0, microsecond=0
            )
            continue
        return cur

def add_business_hours(start_local: dt.datetime, hours: float) -> dt.datetime:
    """
    Deterministic business-hours adder: Mon-Fri, configured hours local time.
    Adds hours strictly across business windows.
    """
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=ZoneInfo(TZ_NAME))

    remaining = hours * 3600.0
    cur = _roll_to_next_business_open(start_local)

    while remaining > 0:
        day_end = cur.replace(hour=_bh_end_h, minute=_bh_end_m, second=0, microsecond=0)
        available = (day_end - cur).total_seconds()
        if remaining <= available:
            return cur + dt.timedelta(seconds=remaining)

        remaining -= available
        cur = (cur + dt.timedelta(days=1)).replace(
            hour=_bh_start_h, minute=_bh_start_m, second=0, microsecond=0
        )
        while cur.weekday() >= 5:
            cur = (cur + dt.timedelta(days=1)).replace(
                hour=_bh_start_h, minute=_bh_start_m, second=0, microsecond=0
            )

    return cur

def _fmt_date_local(d: dt.datetime) -> str:
    return d.strftime("%b %-d")  # e.g. "Feb 25"

def _fmt_as_of_local(d: dt.datetime) -> str:
    return d.strftime("%-I:%M%p").lower() + " CT"  # e.g. "1:01p CT"

def ghl_conversation_link(conversation_id: Optional[str]) -> Optional[str]:
    if not conversation_id or not GHL_LOCATION_ID:
        return None
    return f"{GHL_APP_BASE}/v2/location/{GHL_LOCATION_ID}/conversations/conversations/{conversation_id}"


# ==========================
# GHL API helpers
# ==========================
def _ghl_headers() -> Dict[str, str]:
    if not GHL_TOKEN:
        raise HTTPException(status_code=500, detail="Server missing GHL_TOKEN")
    if not GHL_LOCATION_ID:
        raise HTTPException(status_code=500, detail="Server missing GHL_LOCATION_ID")
    return {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Version": GHL_VERSION,
        "LocationId": GHL_LOCATION_ID,   # <-- THIS is the fix
    }

async def ghl_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = GHL_BASE_URL.rstrip("/") + path
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=_ghl_headers(), params=params or {})
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"GHL GET {path} failed: {r.status_code} {r.text[:300]}")
        return r.json()

async def ghl_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = GHL_BASE_URL.rstrip("/") + path
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=_ghl_headers(), json=payload)
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"GHL POST {path} failed: {r.status_code} {r.text[:300]}")
        return r.json()

async def ghl_list_messages(conversation_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Verified response shape: {'messages': [...], 'traceId': ...}
    (Your container probe showed top keys: ['messages','traceId'])
    """
    data = await ghl_get(f"/conversations/{conversation_id}/messages", params={"limit": limit})

    if isinstance(data, dict):
        msgs = data.get("messages")
        if isinstance(msgs, list):
            return msgs
        # fallback older shapes
        if isinstance(msgs, dict) and isinstance(msgs.get("messages"), list):
            return msgs["messages"]
        if isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data.get("data"), dict) and isinstance(data["data"].get("messages"), list):
            return data["data"]["messages"]
    if isinstance(data, list):
        return data
    return []

async def ghl_send_message(conversation_id: str, contact_id: str, message_text: str) -> Dict[str, Any]:
    """
    LOCKED (verified):
      POST /conversations/messages
      payload:
        type: "SMS"
        message: "<text>"
        conversationId: "<id>"
        contactId: "<id>"
    """
    payload = {
        "type": "SMS",
        "message": message_text,
        "conversationId": conversation_id,
        "contactId": contact_id,
    }
    return await ghl_post("/conversations/messages", payload)


async def ghl_get_contact_name(contact_id: Optional[str]) -> Optional[str]:
    """Best-effort contact name lookup via GHL Contacts API."""
    if not contact_id:
        return None
    try:
        data = await ghl_get(f"/contacts/{contact_id}")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    c = data.get("contact") if isinstance(data.get("contact"), dict) else data
    if isinstance(c, dict):
        # Try standard fields first
        for k in ("name", "fullName", "contactName"):
            v = c.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        
        # Fallback to firstName + lastName
        first = (c.get("firstName") or "").strip()
        last = (c.get("lastName") or "").strip()
        if first or last:
            name = f"{first} {last}".strip()
            return name if name else None
    
    return None

async def ghl_find_conversation_id_for_contact(contact_id: Optional[str], phone: Optional[str]) -> Optional[str]:
    """
    Deterministic: call conversations/search and return the newest conversation id.
    Prefers contact_id; falls back to phone if contact_id missing.

    NOTE: response shape can vary; we normalize common shapes.
    """
    params: Dict[str, Any] = {}
    if contact_id:
        params["contactId"] = contact_id
    elif phone:
        params["phone"] = phone
    else:
        return None

    data = await ghl_get("/conversations/search", params=params)

    if isinstance(data, dict):
        for key in ("conversations", "data", "items"):
            if key in data and isinstance(data[key], list) and data[key]:
                c = data[key][0]
                if isinstance(c, dict):
                    for k in ("id", "conversationId"):
                        v = c.get(k)
                        if isinstance(v, str) and v.strip():
                            return v.strip()
    return None


# ==========================
# Payload extraction helpers
# ==========================
def _normalize_phone(p: Optional[str]) -> Optional[str]:
    if not p:
        return None
    s = str(p).strip()
    s = re.sub(r"[^\d\+]", "", s)
    if s.startswith("00"):
        s = "+" + s[2:]
    if s and s[0] != "+" and len(re.sub(r"\D", "", s)) == 10:
        s = "+1" + re.sub(r"\D", "", s)
    return s

def _extract_text(payload: Dict[str, Any]) -> str:
    for k in ("body", "message", "text", "content", "Message"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # nested dicts (your workflow payload has message.body)
    for k in ("data", "sms", "message", "Message"):
        v = payload.get(k)
        if isinstance(v, dict):
            t = _extract_text(v)
            if t:
                return t
    return ""

def _extract_conversation_id(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("conversationId", "conversation_id", "conversation", "conversationID"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            for kk in ("id", "conversationId"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    return vv.strip()
    d = payload.get("data")
    if isinstance(d, dict):
        return _extract_conversation_id(d)
    return None

def _extract_contact_id(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("contactId", "contact_id", "contact", "contactID"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            vv = v.get("id")
            if isinstance(vv, str) and vv.strip():
                return vv.strip()
    d = payload.get("data")
    if isinstance(d, dict):
        return _extract_contact_id(d)
    return None

def _extract_from_phone(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("from", "fromNumber", "phone", "customerPhone"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return _normalize_phone(v)
    d = payload.get("data")
    if isinstance(d, dict):
        return _extract_from_phone(d)
    return None

def _extract_direction(payload: Dict[str, Any]) -> str:
    for k in ("direction", "type"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    d = payload.get("data")
    if isinstance(d, dict):
        return _extract_direction(d)
    return ""

def _extract_contact_type(payload: Dict[str, Any]) -> Optional[str]:
    for k in ("contactType", "contact_type", "type"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()

    d = payload.get("contact") or payload.get("data")
    if isinstance(d, dict):
        for k in ("contactType", "contact_type", "type"):
            v = d.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return None


def _is_internal_sender(contact_type: Optional[str], contact_id: Optional[str]) -> bool:
    if contact_type and contact_type.lower() == "internal":
        return True
    if contact_id and contact_id in INTERNAL_CONTACT_IDS:
        return True
    return False

def _extract_contact_name(payload: Dict[str, Any]) -> Optional[str]:
    # Try common direct keys
    for k in ("contactName", "fullName", "full_name", "name"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()

    # Try nested objects commonly used by GHL webhooks
    for container_key in ("contact", "data"):
        d = payload.get(container_key)
        if isinstance(d, dict):
            for k in ("contactName", "fullName", "full_name", "name"):
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()

    return None


# ==========================
# Spam helper
# ==========================
def _is_spam(conn: sqlite3.Connection, phone: Optional[str]) -> bool:
    if not phone:
        return False
    row = conn.execute("SELECT 1 FROM spam_phones WHERE phone = ?", (phone,)).fetchone()
    return row is not None

def mark_spam(phone: str) -> None:
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO spam_phones (phone, created_ts) VALUES (?, ?)",
        (phone, _now_local().isoformat())
    )
    conn.commit()
    conn.close()


# ==========================
# KV store helpers
# ==========================
def kv_get(key: str) -> Optional[str]:
    conn = db()
    row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None

def kv_set(key: str, value: str) -> None:
    conn = db()
    conn.execute("INSERT INTO kv_store(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

def _update_issue_meta(issue_id: int, updates: Dict[str, Any]) -> None:
    conn = db()
    row = conn.execute("SELECT meta FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        conn.close()
        return
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    meta.update(updates or {})
    conn.execute("UPDATE issues SET meta=? WHERE id=?", (json.dumps(meta), issue_id))
    conn.commit()
    conn.close()


# ==========================
# Raw event ingestion
# ==========================
async def _parse_request_payload(request: Request) -> Dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    raw_body = await request.body()
    payload: Dict[str, Any] = {
        "_meta": {
            "content_type": content_type,
            "content_length": len(raw_body),
        }
    }

    if "application/json" in content_type and raw_body.strip():
        try:
            payload.update(json.loads(raw_body.decode("utf-8")))
        except Exception as e:
            payload["_meta"]["json_error"] = str(e)
            payload["_raw"] = raw_body.decode("utf-8", errors="replace")
    else:
        try:
            form = await request.form()
            payload.update(dict(form))
        except Exception as e:
            payload["_meta"]["form_error"] = str(e)
            payload["_raw"] = raw_body.decode("utf-8", errors="replace")
    return payload

def _log_raw_event(source: str, payload: Dict[str, Any]) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO raw_events (received_ts, source, payload) VALUES (?, ?, ?)",
        (dt.datetime.utcnow().isoformat(), source, json.dumps(payload))
    )
    conn.commit()
    conn.close()

def _flow_who(contact_name: Optional[str], phone: Optional[str], contact_id: Optional[str]) -> str:
    if isinstance(contact_name, str) and contact_name.strip():
        return contact_name.strip()
    if isinstance(phone, str) and phone.strip():
        p = phone.strip()
        if p.startswith("+1") and len(p) >= 12:
            return "+1***" + p[-4:]
        if len(p) >= 4:
            return "***" + p[-4:]
        return p
    if isinstance(contact_id, str) and contact_id.strip():
        return f"contact:{contact_id.strip()}"
    return "unknown"

def _flow_log(event: str, **fields: Any) -> None:
    if not FLOW_LOG_ENABLED:
        return
    payload = {
        "ts": dt.datetime.now(tz=ZoneInfo(TZ_NAME)).isoformat(),
        "event": event,
    }
    for k, v in fields.items():
        if v is not None:
            payload[k] = v
    print("FLOW " + json.dumps(payload, separators=(",", ":"), ensure_ascii=True))

# ---- Manager LIST pagination (in-memory) ----
# Keyed by manager contact_id. Resets on restart (fine).
_MANAGER_LIST_OFFSETS: dict[str, int] = {}

def _mask_phone(phone: str) -> str:
    p = (phone or "").strip()
    # expects +1XXXXXXXXXX
    if len(p) >= 12 and p.startswith("+1"):
        return "+1***" + p[-4:]
    if len(p) >= 4:
        return "***" + p[-4:]
    return p or "Unknown"

def _fmt_time_local(dt) -> str:
    # dt may already be a datetime; your codebase likely uses aware dt.
    # Keep it simple: match your summary style (e.g., 10:19pm)
    try:
        return dt.strftime("%-I:%M%p").lower()
    except Exception:
        try:
            return dt.strftime("%I:%M%p").lstrip("0").lower()
        except Exception:
            return str(dt)

def _format_issue_line_like_summary(r: dict) -> str:
    """
    Formats a single issue line in the same style as the summary:
    #ID NameOrMaskedPhone — <last_time> | due <due_time> [in=N for SMS]
    """
    iid = r.get("id")
    phone = r.get("phone") or ""
    contact_name = r.get("contact_name") or r.get("contact") or ""  # depending on your schema
    label = contact_name if contact_name and not contact_name.startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")) else ""
    who = label if label else _mask_phone(phone)

    last_dt = r.get("last_in") or r.get("last_message_at") or r.get("last_seen_at")
    due_dt = r.get("due_at") or r.get("due")

    last_s = _fmt_time_local(last_dt) if last_dt else "?"
    due_s = _fmt_time_local(due_dt) if due_dt else "?"

    issue_type = r.get("issue_type") or r.get("type")  # 1=CALL? depends on your code
    # In your existing output you label as [CALL] or [SMS]. We only need "in=N" for SMS.
    inbound_count = r.get("inbound_count") or r.get("inbound") or 0

    suffix = ""
    # Only show in=N for texts (matches your summary)
    # If your schema uses something else to tell SMS vs CALL, adjust this predicate.
    if str(r.get("channel") or r.get("kind") or r.get("medium") or "").lower() in ("sms", "text"):
        suffix = f" in={inbound_count}"
    else:
        # fallback: if your existing issues store something like r["is_sms"] or message types
        if r.get("is_sms") or r.get("sms") or r.get("text"):
            suffix = f" in={inbound_count}"

    return f"#{iid} {who} — {last_s} | due {due_s}{suffix}".strip()

def _render_list_like_summary(issues: list[dict], total_open: int, offset: int, limit: int) -> str:
    """
    Render as:
    OPEN (total) showing X-Y
    Calls (N):
    ...
    Texts (N):
    ...
    More (if available)
    """
    calls = []
    texts = []

    for r in issues:
        # Decide if CALL vs SMS the same way your summary does.
        # Adjust these predicates to match your schema.
        is_sms = False
        t = (r.get("channel") or r.get("kind") or r.get("medium") or "").lower()
        if t in ("sms", "text"):
            is_sms = True
        if r.get("is_sms") or r.get("sms") or r.get("text"):
            is_sms = True

        line = _format_issue_line_like_summary(r)
        (texts if is_sms else calls).append(line)

    start = offset + 1 if total_open else 0
    end = min(offset + limit, total_open)

    lines = []
    lines.append(f"OPEN ({total_open}) — showing {start}-{end}")
    if calls:
        lines.append(f"Calls ({len(calls)}):")
        lines.extend(calls)
    if texts:
        lines.append(f"Texts ({len(texts)}):")
        lines.extend(texts)

    if end < total_open:
        lines.append("Reply: More")
    else:
        lines.append("End of list. Reply: List")

    return "\n".join(lines)

async def handle_command(text: str, command_contact_id: Optional[str], command_from_phone: Optional[str]) -> Dict[str, Any]:
    """
    Manager command parser (internal only, invoked from inbound_sms when contact_type == "internal").

    Supported (case-insensitive; '#' optional; commas allowed):
      List
      Open 3
      Resolve 3 5 6   (or: Resolve 3,5,6)
      Resolve <phone/contactId/name>   (existing behavior)
      Spam 7          (marks issue SPAM + adds phone to spam list if present)
      Spam <phone>    (adds phone to spam list + resolves by phone)
      Note 3 <text>
    Optional prefix:
      Sentinel <command...>
    """

    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "ignored": "empty"}

    # Optional "Sentinel" prefix to reduce friction (not required)
    raw = re.sub(r"^\s*sentinel\s+", "", raw, flags=re.IGNORECASE)

    # Normalize commas into spaces so "3,5,6" works
    raw = raw.replace(",", " ")

    parts = raw.split()
    if not parts:
        return {"ok": False, "ignored": "empty"}

    cmd = parts[0].strip().lower()
    args = parts[1:]

    # Only treat known first-words as commands; otherwise ignore (so normal internal texts don't get eaten)
    known = {"list", "more", "open", "resolve", "spam", "note"}
    if cmd not in known:
        return {"ok": False, "ignored": "not_a_command"}

    def _parse_ids(tokens: List[str]) -> List[int]:
        ids: List[int] = []
        for t in tokens:
            tt = t.strip()
            if tt.startswith("#"):
                tt = tt[1:]
            if tt.isdigit():
                ids.append(int(tt))
        # de-dupe while preserving order
        seen = set()
        out: List[int] = []
        for i in ids:
            if i not in seen:
                out.append(i)
                seen.add(i)
        return out

    if cmd == "list":
        if not command_contact_id:
            return {"ok": False, "error": "Missing manager contact id"}

        limit = 5
        offset = 0
        _MANAGER_LIST_OFFSETS[command_contact_id] = offset

        rows, total = list_open_issues(limit=limit, offset=offset)
        if total == 0:
            return {"ok": True, "cmd": "LIST", "text": "No OPEN issues."}

        for r in rows:
            if not (r.get("contact_name") or "").strip() and r.get("contact_id"):
                fetched = await ghl_get_contact_name(r["contact_id"])
                if fetched:
                    _set_issue_contact_name(r["id"], fetched)
                    r["contact_name"] = fetched

        body = _render_list_like_summary(rows, total_open=total, offset=offset, limit=limit)
        return {"ok": True, "cmd": "LIST", "text": body}

    if cmd == "more":
        if not command_contact_id:
            return {"ok": False, "error": "Missing manager contact id"}

        limit = 5
        offset = _MANAGER_LIST_OFFSETS.get(command_contact_id, 0) + limit
        _MANAGER_LIST_OFFSETS[command_contact_id] = offset

        rows, total = list_open_issues(limit=limit, offset=offset)
        if not rows:
            _MANAGER_LIST_OFFSETS[command_contact_id] = 0
            return {"ok": True, "cmd": "MORE", "text": "No more OPEN issues. Reply: List"}

        for r in rows:
            if not (r.get("contact_name") or "").strip() and r.get("contact_id"):
                fetched = await ghl_get_contact_name(r["contact_id"])
                if fetched:
                    _set_issue_contact_name(r["id"], fetched)
                    r["contact_name"] = fetched

        body = _render_list_like_summary(rows, total_open=total, offset=offset, limit=limit)
        return {"ok": True, "cmd": "MORE", "text": body}

    if cmd == "open":
        if not args:
            return {"ok": False, "error": "Usage: Open <id>"}
        iid = _parse_issue_id(args[0])
        if not iid:
            return {"ok": False, "error": "Invalid issue id"}
        r = get_issue_by_id(iid)
        if not r:
            return {"ok": False, "error": "Issue not found"}
        name = _display_name(r)
        link = ghl_conversation_link(r["conversation_id"])
        txt = f"{name}: {link}" if link else f"{name}: conversation_id={r['conversation_id'] or '-'}"
        return {"ok": True, "cmd": "OPEN", "id": iid, "text": txt}

    if cmd == "note":
        if len(args) < 2:
            return {"ok": False, "error": "Usage: Note <id> <text>"}
        iid = _parse_issue_id(args[0])
        if not iid:
            return {"ok": False, "error": "Invalid issue id"}
        note_text = " ".join(args[1:]).strip()
        ok = add_note(iid, note_text)
        return {"ok": ok, "cmd": "NOTE", "id": iid, "text": ("Noted." if ok else "Issue not found.")}

    if cmd == "resolve":
        if not args:
            return {"ok": False, "error": "Usage: Resolve <id...>  OR  Resolve <phone/contactId/name>"}

        ids = _parse_ids(args)
        if ids:
            changed: List[int] = []
            for iid in ids:
                if resolve_by_id(iid, status="RESOLVED") > 0:
                    changed.append(iid)
            if changed:
                return {"ok": True, "cmd": "RESOLVE", "ids": changed, "text": f"Sentinel: Resolved {', '.join(str(x) for x in changed)}."}
            return {"ok": True, "cmd": "RESOLVE", "ids": ids, "text": "Sentinel: No matching OPEN issues for those IDs."}

        # fallback: existing target resolver (phone/contactId/name)
        target = " ".join(args).strip()
        resolved = resolve_target(target)
        return {"ok": True, "cmd": "RESOLVE", "resolved": resolved, "target": target, "text": f"Sentinel: Resolved {resolved} issue(s) for '{target}'."}

    if cmd == "spam":
        if not args:
            return {"ok": False, "error": "Usage: Spam <id...>  OR  Spam <phone>"}

        ids = _parse_ids(args)
        if ids:
            marked: List[int] = []
            for iid in ids:
                r = get_issue_by_id(iid)
                if r and r["phone"]:
                    try:
                        mark_spam(r["phone"])
                    except Exception:
                        pass
                if resolve_by_id(iid, status="SPAM") > 0:
                    marked.append(iid)
            if marked:
                return {"ok": True, "cmd": "SPAM", "ids": marked, "text": f"Sentinel: Marked SPAM {', '.join(str(x) for x in marked)}."}
            return {"ok": True, "cmd": "SPAM", "ids": ids, "text": "Sentinel: No matching OPEN issues for those IDs."}

        # phone spam fallback
        phone = _normalize_phone(args[0])
        if not phone:
            return {"ok": False, "error": "Invalid phone or IDs"}
        mark_spam(phone)
        resolve_by_phone(phone, status="SPAM")
        return {"ok": True, "cmd": "SPAM", "phone": phone, "text": f"Sentinel: Marked SPAM {phone}."}

    return {"ok": False, "error": "Unknown command"}

def _parse_issue_id(token: str) -> Optional[int]:
    t = (token or "").strip()
    if t.startswith("#"):
        t = t[1:]
    return int(t) if t.isdigit() else None

def get_issue_by_id(issue_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    conn.close()
    return row

def resolve_by_id(issue_id: int, status: str = "RESOLVED") -> int:
    conn = db()
    now = _now_local().isoformat()
    cur = conn.execute(
        "UPDATE issues SET status=?, resolved_ts=? WHERE status='OPEN' AND id=?",
        (status, now, issue_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount

def add_note(issue_id: int, note: str) -> bool:
    conn = db()
    row = conn.execute("SELECT meta FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        conn.close()
        return False
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    notes = meta.get("notes") or []
    notes.append({"ts": _now_local().isoformat(), "text": note[:500]})
    meta["notes"] = notes
    conn.execute("UPDATE issues SET meta=? WHERE id=?", (json.dumps(meta), issue_id))
    conn.commit()
    conn.close()
    return True


# ---- Manager LIST paging state (in-memory) ----
_MANAGER_LIST_OFFSETS: dict[str, int] = {}

def _mask_phone(phone: str) -> str:
    p = (phone or "").strip()
    if p.startswith("+1") and len(p) >= 12:
        return "+1***" + p[-4:]
    if len(p) >= 4:
        return "***" + p[-4:]
    return p or "Unknown"

def _fmt_hhmm_ampm(value) -> str:
    """
    Convert a datetime or ISO-ish timestamp to 'h:mmap' like the summary (e.g., 3:41pm).
    Accepts:
      - datetime
      - ISO strings (with/without timezone)
      - sqlite-style strings 'YYYY-MM-DD HH:MM:SS'
    """
    if not value:
        return "?"

    parsed = None

    # already a datetime?
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        s = str(value).strip()
        if not s:
            return "?"
        try:
            # Handles '2026-02-25T15:41:10.213158-06:00' and many variants
            parsed = dt.datetime.fromisoformat(s)
        except Exception:
            # Try sqlite style 'YYYY-MM-DD HH:MM:SS'
            try:
                parsed = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                # Try without seconds
                try:
                    parsed = dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
                except Exception:
                    return "?"

    try:
        return parsed.strftime("%-I:%M%p").lower()
    except Exception:
        return parsed.strftime("%I:%M%p").lstrip("0").lower()

def list_open_issues(limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """
    Returns (rows, total_open) ordered by due_ts ASC.
    Rows are dicts with the columns we need for summary-like formatting.
    """
    conn = db()
    total = conn.execute("""
        SELECT COUNT(*) AS n
        FROM issues
        WHERE status='OPEN'
    """).fetchone()["n"]

    rows = conn.execute("""
        SELECT id, issue_type, phone, contact_id, contact_name, created_ts, due_ts, inbound_count, last_inbound_ts
        FROM issues
        WHERE status='OPEN'
        ORDER BY due_ts ASC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    conn.close()

    # sqlite Row -> dict
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "issue_type": r["issue_type"],  # "CALL" or "SMS" in your existing output
            "phone": r["phone"],
            "contact_id": r["contact_id"],
            "contact_name": r["contact_name"],
            "created_ts": r["created_ts"],
            "due_ts": r["due_ts"],
            "inbound_count": r["inbound_count"] if r["inbound_count"] is not None else 0,
            "last_inbound_ts": r["last_inbound_ts"] or r["created_ts"],
        })
    return out, int(total)

def _render_list_like_summary(rows: list[dict], total_open: int, offset: int, limit: int) -> str:
    """
    Summary-like list output, 5 at a time, split into Calls/Text like summary.
    """
    calls = []
    texts = []

    for r in rows:
        iid = r["id"]
        it = (r.get("issue_type") or "").upper()
        phone = r.get("phone") or ""
        name = (r.get("contact_name") or "").strip()
        who = name if name else _mask_phone(phone)

        last_s = _fmt_hhmm_ampm(r.get("last_inbound_ts") or "")
        due_s = _fmt_hhmm_ampm(r.get("due_ts") or "")

        line = f"#{iid} {who} — {last_s} | due {due_s}"
        if it == "SMS":
            n = int(r.get("inbound_count", 0) or 0)
            if n > 1:
                line += f" ({n})"
            texts.append(line)
        else:
            calls.append(line)

    start = offset + 1 if total_open else 0
    end = min(offset + limit, total_open)

    lines = [f"OPEN ({total_open}) — showing {start}-{end}"]

    if calls:
        lines.append(f"Calls ({len(calls)}):")
        lines.extend(calls)

    if texts:
        lines.append(f"Texts ({len(texts)}):")
        lines.extend(texts)

    if end < total_open:
        lines.append("Reply: More")
    else:
        lines.append("End of list. Reply: List")

    return "\n".join(lines)

def _set_issue_contact_name(issue_id: int, name: str) -> None:
    if not name:
        return
    conn = db()
    conn.execute(
        "UPDATE issues SET contact_name=? WHERE id=? AND (contact_name IS NULL OR contact_name='')",
        (name, issue_id),
    )
    conn.commit()
    conn.close()

def resolve_by_phone(phone: str, status: str = "RESOLVED") -> int:
    conn = db()
    now = _now_local().isoformat()
    cur = conn.execute("""
        UPDATE issues
        SET status=?, resolved_ts=?
        WHERE status='OPEN' AND phone=?
    """, (status, now, phone))
    conn.commit()
    conn.close()
    return cur.rowcount

def resolve_by_contact_id(contact_id: str, status: str = "RESOLVED") -> int:
    conn = db()
    now = _now_local().isoformat()
    cur = conn.execute("""
        UPDATE issues
        SET status=?, resolved_ts=?
        WHERE status='OPEN' AND contact_id=?
    """, (status, now, contact_id))
    conn.commit()
    conn.close()
    return cur.rowcount

def resolve_by_name(name: str, status: str = "RESOLVED") -> int:
    name_l = name.strip().lower()
    if not name_l:
        return 0

    conn = db()
    rows = conn.execute("SELECT id, meta FROM issues WHERE status='OPEN'").fetchall()
    matched_ids: List[int] = []

    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        cn = (meta.get("contact_name") or "").lower()
        if cn and name_l in cn:
            matched_ids.append(r["id"])

    now = _now_local().isoformat()
    if matched_ids:
        q = "UPDATE issues SET status=?, resolved_ts=? WHERE id IN (%s)" % ",".join(["?"] * len(matched_ids))
        conn.execute(q, [status, now] + matched_ids)
        conn.commit()

    conn.close()
    return len(matched_ids)

def _looks_like_contact_id(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{10,}", s))

def resolve_target(target: str) -> int:
    t = target.strip()
    phone = _normalize_phone(t)
    if phone:
        return resolve_by_phone(phone)
    if _looks_like_contact_id(t):
        return resolve_by_contact_id(t)
    return resolve_by_name(t)


# ==========================
# Webhooks
# ==========================
@app.post("/webhook/ghl")
async def ghl_webhook_raw(request: Request):
    _auth_or_401(request)
    payload = await _parse_request_payload(request)
    _log_raw_event("ghl_raw", payload)
    return {"received": True}

@app.post("/webhook/ghl/inbound_sms")
async def inbound_sms(request: Request):
    """
    Final SMS Logic (Locked):

    Inbound SMS webhook:
      - Internal manager SMS:
          - Recognized command words (list/open/resolve/spam/note/more) are parsed by handle_command
          - Non-command internal texts are ignored
      - Customer inbound:
          - If no OPEN SMS issue for conversation (preferred) -> create issue
            store first_inbound_ts, conversation_id, inbound_count=1
            DO NOT reset due_ts later
          - Else update last_inbound_ts and inbound_count += 1

    conversation_id is looked up via /conversations/search using contact_id (preferred) or phone.
    """
    _auth_or_401(request)
    payload = await _parse_request_payload(request)
    _log_raw_event("inbound_sms", payload)

    text = _extract_text(payload)
    contact_id = _extract_contact_id(payload)
    from_phone = _extract_from_phone(payload)
    conversation_id = _extract_conversation_id(payload)

    contact_name = _extract_contact_name(payload)
    if not contact_name and contact_id:
        try:
            contact_name = await ghl_get_contact_name(contact_id)
        except Exception:
            contact_name = None
    direction = _extract_direction(payload)
    contact_type = _extract_contact_type(payload)
    is_internal = _is_internal_sender(contact_type, contact_id)
    who = _flow_who(contact_name, from_phone, contact_id)

    now_local = _now_local()
    created_ts = now_local.isoformat()
    due_ts = add_business_hours(now_local, SMS_SLA_HOURS).isoformat()

    # If conversation_id wasn't in the payload, resolve via GHL search (do early so grace check can work)
    if not conversation_id:
        try:
            conversation_id = await ghl_find_conversation_id_for_contact(contact_id, from_phone)
        except Exception:
            conversation_id = None

    # Track internal activity so customer replies within a grace window don't create false positives
    if conversation_id and is_internal:
        set_last_internal_outbound(conversation_id, created_ts, contact_id)

    if direction in ("outbound", "outgoing"):
        _flow_log("sms.ignored_outbound", who=who, contact_id=contact_id, conversation_id=conversation_id)
        return {"received": True, "ignored": "outbound"}

    # Internal manager commands (no "SENTINEL" prefix required; handled by handle_command)
    if is_internal:
        result = await handle_command(text=text, command_contact_id=contact_id, command_from_phone=from_phone)

        # Only respond if it was actually a recognized command
        if result.get("ok") and result.get("text") and contact_id:
            try:
                conv_id = await _manager_conversation_for_contact(contact_id)
                if conv_id:
                    await ghl_send_message(conv_id, contact_id, result["text"])
            except Exception:
                pass
            _flow_log("sms.internal_command", who=who, contact_id=contact_id, command=result.get("cmd"))
            return {"received": True, "command": True, "result": result}

        # internal non-command
        _flow_log("sms.ignored_internal_non_command", who=who, contact_id=contact_id)
        return {"received": True, "ignored": "internal_non_command"}

    # Customer "ack/close-out" after a staff reply:
    # If we have evidence of a recent *staff* outbound in this conversation, suppress low-signal acknowledgements
    # like "thanks", "👍", "fixed it", etc. This prevents false-positive issues when the customer replies last.
    if conversation_id and ACK_CLOSE_ENABLED:
        last_internal_ts = get_last_internal_outbound(conversation_id)
        last_dt = _parse_iso_dt(last_internal_ts)
        if last_dt:
            last_dt_local = last_dt.astimezone(ZoneInfo(TZ_NAME)) if last_dt.tzinfo else last_dt.replace(tzinfo=ZoneInfo(TZ_NAME))
            if ACK_CLOSE_WINDOW_MODE == "eod":
                window_end = _business_day_end_for(last_dt_local)
                within_window = (now_local >= last_dt_local) and (now_local <= window_end)
            else:
                delta = now_local - last_dt_local
                within_window = 0 <= delta.total_seconds() <= (ACK_CLOSE_WINDOW_HOURS * 3600.0)

            if within_window and _is_ack_closeout(text):
                _flow_log(
                    "sms.ignored_ack_closeout",
                    who=who,
                    contact_id=contact_id,
                    conversation_id=conversation_id,
                )
                return {"received": True, "ignored": "ack_closeout_after_staff_reply"}

    conn = db()

    row = None
    if conversation_id:
        row = conn.execute(
            "SELECT * FROM issues WHERE status IN ('PENDING','OPEN') AND issue_type='SMS' AND conversation_id=? ORDER BY id DESC LIMIT 1",
            (conversation_id,)
        ).fetchone()

    if row is None and from_phone:
        row = conn.execute(
            "SELECT * FROM issues WHERE status IN ('PENDING','OPEN') AND issue_type='SMS' AND phone=? ORDER BY id DESC LIMIT 1",
            (from_phone,)
        ).fetchone()

    if row is None:
        meta = {
            "last_text": text[:500],
            "source": "inbound_sms_webhook",
        }
        if contact_name:
            meta["contact_name"] = contact_name
        cur = conn.execute("""
            INSERT INTO issues
              (issue_type, contact_id, phone, contact_name, created_ts, due_ts, status, meta,
               first_inbound_ts, last_inbound_ts, inbound_count, outbound_count, conversation_id)
            VALUES
              ('SMS', ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, 1, 0, ?)
        """, (
            contact_id, from_phone, contact_name or None, created_ts, due_ts, json.dumps(meta),
            created_ts, created_ts, conversation_id
        ))
        _flow_log(
            "sms.issue_created",
            issue_id=cur.lastrowid,
            who=who,
            contact_id=contact_id,
            conversation_id=conversation_id,
            status="PENDING",
            due_ts=due_ts,
        )
    else:
        meta = {}
        try:
            if row["meta"]:
                meta = json.loads(row["meta"])
        except Exception:
            meta = {}

        meta["last_text"] = text[:500]
        meta["updated_by"] = "inbound_sms_webhook"

        if contact_name and not meta.get("contact_name"):
            meta["contact_name"] = contact_name

        conn.execute("""
            UPDATE issues
            SET last_inbound_ts=?,
                inbound_count=COALESCE(inbound_count,0)+1,
                contact_id=COALESCE(contact_id, ?),
                phone=COALESCE(phone, ?),
                conversation_id=COALESCE(conversation_id, ?),
                contact_name=CASE WHEN (contact_name IS NULL OR contact_name='') THEN ? ELSE contact_name END,
                meta=?
            WHERE id=?
        """, (created_ts, contact_id, from_phone, conversation_id, contact_name or None, json.dumps(meta), row["id"]))
        _flow_log(
            "sms.issue_updated",
            issue_id=row["id"],
            who=who,
            contact_id=contact_id,
            conversation_id=conversation_id,
            status=row["status"],
        )

    conn.commit()
    conn.close()
    return {"received": True, "issue_created_or_updated": True}

@app.post("/webhook/ghl/unanswered_call")
async def unanswered_call(request: Request):
    """
    Deterministic CALL issues only from voicemail_route=tech_sentinel controlled signal.
    """
    _auth_or_401(request)
    payload = await _parse_request_payload(request)
    _log_raw_event("unanswered_call", payload)

    vr = payload.get("voicemail_route")
    if isinstance(vr, list):
        routes = [str(x) for x in vr]
    else:
        routes = [str(vr)] if vr is not None else []

    if "tech_sentinel" not in routes:
        return {"received": True, "ignored": "voicemail_route_not_tech_sentinel"}

    contact_id = _extract_contact_id(payload)
    from_phone = _extract_from_phone(payload)
    conversation_id = _extract_conversation_id(payload)

    contact_name = _extract_contact_name(payload)
    if not contact_name:
        try:
            contact_name = await ghl_get_contact_name(contact_id)
        except Exception:
            contact_name = None
    if not conversation_id:
        try:
            conversation_id = await ghl_find_conversation_id_for_contact(contact_id, from_phone)
        except Exception:
            conversation_id = None
    who = _flow_who(contact_name, from_phone, contact_id)

    conn = db()
    if _is_spam(conn, from_phone):
        conn.close()
        _flow_log("call.ignored_spam", who=who, contact_id=contact_id, conversation_id=conversation_id)
        return {"received": True, "ignored": "spam_phone"}

    now_local = _now_local()
    created_ts = now_local.isoformat()
    due_ts = add_business_hours(now_local, CALL_SLA_HOURS).isoformat()

    meta = {"source": "voicemail_route=tech_sentinel"}
    if contact_name:
        meta["contact_name"] = contact_name
    cur = conn.execute("""
        INSERT INTO issues (
            issue_type, contact_id, phone, contact_name, created_ts, due_ts, status, meta,
            conversation_id, first_inbound_ts, last_inbound_ts, inbound_count, outbound_count
        )
        VALUES ('CALL', ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, 1, 0)
    """, (
        contact_id, from_phone, contact_name or None, created_ts, due_ts, json.dumps(meta),
        conversation_id, created_ts, created_ts
    ))
    conn.commit()
    conn.close()
    _flow_log(
        "call.issue_created",
        issue_id=cur.lastrowid,
        who=who,
        contact_id=contact_id,
        conversation_id=conversation_id,
        status="PENDING",
        due_ts=due_ts,
    )
    return {"received": True, "issue_created": True}


# ==========================
# Polling resolver (SMS)
# ==========================
def _msg_ts(m: Dict[str, Any]) -> Optional[dt.datetime]:
    v = m.get("dateAdded")
    if isinstance(v, str) and v:
        try:
            return dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    return None

def _msg_direction(m: Dict[str, Any]) -> str:
    v = m.get("direction")
    if isinstance(v, str) and v:
        return v.lower()
    return ""

def _msg_text(m: Dict[str, Any]) -> str:
    if not isinstance(m, dict):
        return ""
    for k in ("body", "message", "text", "content"):
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""

def _internal_user_ids() -> set:
    raw = os.getenv("INTERNAL_USER_IDS", "").strip()
    if not raw:
        return set()
    return {x.strip() for x in raw.split(",") if x.strip()}

def _msg_is_staff_outbound(m: Dict[str, Any]) -> bool:
    """
    Returns True only for a real staff reply:
      - direction == outbound
      - userId is present (excludes workflow automation which has no userId)
      - userId is in INTERNAL_USER_IDS allowlist
    Strict mode only: INTERNAL_USER_IDS must be configured for any auto-resolve.
    """
    if not isinstance(m, dict):
        return False
    if _msg_direction(m) != "outbound":
        return False
    uid = m.get("userId")
    if not uid:
        return False
    allow = _internal_user_ids()
    if not allow:
        return False
    return uid in allow


def _set_issue_status(issue_id: int, status: str) -> None:
    conn = db()
    conn.execute("UPDATE issues SET status=? WHERE id=?", (status, issue_id))
    conn.commit()
    conn.close()


def _has_outbound_after(msgs: List[Dict[str, Any]], first_inbound_ts: str) -> bool:
    cutoff = _parse_iso_dt(first_inbound_ts)
    if not cutoff:
        return False

    for m in msgs or []:
        direction = _msg_direction(m)
        if direction != "outbound":
            continue
        mts = _msg_ts(m)
        if not mts:
            continue
        try:
            cutoff_utc = cutoff.astimezone(dt.timezone.utc) if cutoff.tzinfo else cutoff.replace(
                tzinfo=ZoneInfo(TZ_NAME)
            ).astimezone(dt.timezone.utc)
            if mts.astimezone(dt.timezone.utc) > cutoff_utc:
                return True
        except Exception:
            continue

    return False

@app.post("/jobs/poll_resolver")
async def poll_resolver(request: Request, limit: int = 200):
    """
    For each OPEN SMS issue:
      Fetch messages for conversation_id
      Resolve if ANY outbound where dateAdded > first_inbound_ts
    """
    _auth_or_401(request)

    conn = db()
    rows = conn.execute("""
        SELECT id, conversation_id, first_inbound_ts, outbound_count
        FROM issues
        WHERE status IN ('OPEN','PENDING')
          AND issue_type='SMS'
          AND conversation_id IS NOT NULL
        ORDER BY due_ts ASC
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()

    checked = 0
    resolved = 0
    updated_counts = 0

    for r in rows:
        checked += 1
        issue_id = r["id"]
        conv_id = r["conversation_id"]
        if not conv_id:
            continue

        try:
            msgs = await ghl_list_messages(conv_id, limit=50)
        except HTTPException:
            continue

        try:
            fi = dt.datetime.fromisoformat((r["first_inbound_ts"] or "").replace("Z", "+00:00"))
        except Exception:
            fi = None

        outbound_after = False
        out_count = 0
        latest_staff_ts: Optional[dt.datetime] = None
        latest_staff_uid: Optional[str] = None

        for m in msgs:
            if _msg_is_staff_outbound(m):
                out_count += 1

                mts0 = _msg_ts(m)
                if mts0 is not None:
                    if latest_staff_ts is None or mts0 > latest_staff_ts:
                        latest_staff_ts = mts0
                        latest_staff_uid = str(m.get("userId") or "")

                if fi is not None:
                    mts = _msg_ts(m)
                    if mts is None:
                        continue
                    try:
                        # compare as UTC
                        fi_utc = fi.astimezone(dt.timezone.utc) if fi.tzinfo else fi.replace(tzinfo=ZoneInfo(TZ_NAME)).astimezone(dt.timezone.utc)
                        if mts.astimezone(dt.timezone.utc) > fi_utc:
                            outbound_after = True
                    except Exception:
                        pass

        if latest_staff_ts is not None:
            try:
                set_last_internal_outbound(
                    conv_id,
                    latest_staff_ts.astimezone(ZoneInfo(TZ_NAME)).isoformat(),
                    latest_staff_uid or None,
                )
            except Exception:
                pass

        conn2 = db()
        prev_out = r["outbound_count"] if r["outbound_count"] is not None else 0
        if out_count != prev_out:
            conn2.execute("UPDATE issues SET outbound_count=? WHERE id=?", (out_count, issue_id))
            conn2.commit()
            updated_counts += 1

        if outbound_after:
            now = _now_local().isoformat()
            conn2.execute("""
                UPDATE issues
                SET status='RESOLVED', resolved_ts=?
                WHERE id=? AND status IN ('OPEN','PENDING')
            """, (now, issue_id))
            conn2.commit()
            resolved += 1
            _flow_log(
                "sms.auto_resolved",
                issue_id=issue_id,
                conversation_id=conv_id,
                via="poll_resolver",
            )

        conn2.close()

    return {"job": "poll_resolver", "checked": checked, "resolved": resolved, "updated_counts": updated_counts}



# ==========================
# AI follow-up gate helpers (optional)
# ==========================
_AI_GATE_SCHEMA = {
    "name": "follow_up_gate",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "needs_follow_up": {"type": "string", "enum": ["YES", "NO"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3
            },
        },
        "required": ["needs_follow_up", "confidence", "evidence"],
    },
}

def _ai_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

def _ai_gate_db_get(conversation_id: str) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute(
        "SELECT * FROM conversation_ai_gate WHERE conversation_id=?",
        (conversation_id,),
    ).fetchone()
    conn.close()
    return row

def _ai_gate_db_put(conversation_id: str, last_msg_ts: str, result: Dict[str, Any]) -> None:
    conn = db()
    conn.execute(
        '''
        INSERT INTO conversation_ai_gate
          (conversation_id, last_msg_ts, needs_follow_up, confidence, evidence_json, model, created_ts)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(conversation_id) DO UPDATE SET
          last_msg_ts=excluded.last_msg_ts,
          needs_follow_up=excluded.needs_follow_up,
          confidence=excluded.confidence,
          evidence_json=excluded.evidence_json,
          model=excluded.model,
          created_ts=excluded.created_ts
        ''',
        (
            conversation_id,
            last_msg_ts,
            str(result.get("needs_follow_up") or "YES"),
            float(result.get("confidence") or 0.0),
            json.dumps(result.get("evidence") or []),
            AI_GATE_MODEL,
            _now_local().isoformat(),
        ),
    )
    conn.commit()
    conn.close()

def _select_context_window(msgs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    '''
    Choose a small, recent slice to avoid mixing multiple mini-conversations.

    Strategy (deterministic):
      - Walk backwards from newest message
      - Stop if we hit a long silence gap (AI_GATE_GAP_HOURS)
      - Also stop once we've included the most recent staff outbound (boundary) AND
        there is at least one newer message after it (i.e., the customer replied)
      - Always cap at AI_GATE_MAX_MESSAGES
    '''
    items: List[Tuple[dt.datetime, Dict[str, Any]]] = []
    for m in msgs or []:
        ts = _msg_ts(m)
        if ts is None:
            continue
        items.append((ts, m))
    items.sort(key=lambda x: x[0])
    if not items:
        return []

    selected: List[Tuple[dt.datetime, Dict[str, Any]]] = []
    last_ts: Optional[dt.datetime] = None
    saw_newer_than_staff = False

    for ts, m in reversed(items):
        if last_ts is not None:
            gap = (last_ts - ts).total_seconds()
            if gap >= (AI_GATE_GAP_HOURS * 3600.0):
                break

        selected.append((ts, m))
        last_ts = ts

        # boundary: include most recent staff outbound, then stop
        if _msg_is_staff_outbound(m):
            if saw_newer_than_staff:
                break
        else:
            saw_newer_than_staff = True

        if len(selected) >= AI_GATE_MAX_MESSAGES:
            break

    selected.reverse()
    return [m for _, m in selected]

def _build_ai_transcript(window: List[Dict[str, Any]]) -> str:
    def _redact_pii(s: str) -> str:
        t = s or ""
        if not AI_GATE_REDACT_PII:
            return t
        t = re.sub(r"\b[\w.\-+%]+@[\w.\-]+\.[A-Za-z]{2,}\b", "[EMAIL]", t)
        t = re.sub(r"https?://\S+", "[URL]", t)
        # lenient phone-like matcher (supports +1, spaces, dashes, parens)
        t = re.sub(r"\+?\d[\d\-\(\) ]{7,}\d", "[PHONE]", t)
        return t

    lines: List[str] = []
    for m in window or []:
        role = "INTERNAL" if _msg_is_staff_outbound(m) else "CUSTOMER"
        txt = (_msg_text(m) or "").replace("\n", " ").strip()
        if not txt:
            continue
        txt = _redact_pii(txt)
        if len(txt) > 500:
            txt = txt[:500] + "…"
        lines.append(f"[{role}] {txt}")
    return "\n".join(lines)

async def ai_gate_classify(conversation_id: str, msgs: List[Dict[str, Any]]) -> Dict[str, Any]:
    '''
    Returns {"needs_follow_up":"YES|NO","confidence":float,"evidence":[...]}.

    Fail-open behavior:
      - If anything goes wrong (missing key, API failure, JSON parse), return YES with low confidence.
      - We only suppress escalation when we get a confident NO.
    '''
    if not AI_GATE_ENABLED:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai gate disabled"]}

    if not OPENAI_API_KEY:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["missing OPENAI_API_KEY"]}

    window = _select_context_window(msgs)
    if not window:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["no messages available"]}

    last_dt = _msg_ts(window[-1])
    last_msg_ts = last_dt.astimezone(dt.timezone.utc).isoformat() if last_dt else _now_local().astimezone(dt.timezone.utc).isoformat()

    cached = _ai_gate_db_get(conversation_id)
    if cached and str(cached["last_msg_ts"]) == last_msg_ts:
        try:
            return {
                "needs_follow_up": str(cached["needs_follow_up"]),
                "confidence": float(cached["confidence"]),
                "evidence": json.loads(cached["evidence_json"] or "[]"),
                "cached": True,
            }
        except Exception:
            pass

    transcript = _build_ai_transcript(window)
    if not (transcript or "").strip():
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["empty transcript"]}
    sys = (
        "You are a classifier for a pool service business SMS thread. "
        "Decide if the business owes a follow-up to the customer. "
        "Bias toward YES if uncertain (missing a waiting customer is worse than a false alarm). "
        "Return only valid JSON matching the schema."
    )
    user = (
        "Definitions:\n"
        "- FOLLOW-UP NEEDED means the customer is waiting on us for an answer, action, or scheduling.\n"
        "- FOLLOW-UP NOT NEEDED means the thread is resolved or the customer is only acknowledging (thanks/ok/fixed it).\n\n"
        "Messages (most recent last):\n"
        f"{transcript}"
    )

    payload = {
        "model": AI_GATE_MODEL,
        "input": [
            {"role": "system", "content": sys},
            {"role": "user", "content": user},
        ],
        "text": {"format": {"type": "json_schema", "json_schema": _AI_GATE_SCHEMA}},
        "store": False,
    }

    try:
        async with httpx.AsyncClient(timeout=AI_GATE_TIMEOUT_SECONDS) as client:
            r = await client.post(f"{OPENAI_BASE_URL}/responses", headers=_ai_headers(), json=payload)
            if r.status_code >= 400:
                return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": [f"ai error {r.status_code}"]}
            data = r.json()

        txt = (data.get("output_text") or "").strip()
        if not txt:
            for item in (data.get("output") or []):
                if item.get("type") == "message":
                    for part in (item.get("content") or []):
                        if part.get("type") == "output_text" and part.get("text"):
                            txt = str(part.get("text") or "").strip()
                            break
                if txt:
                    break
        if not txt:
            return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai empty response"]}

        result = json.loads(txt)
        nf = str(result.get("needs_follow_up") or "YES").upper()
        if nf not in ("YES", "NO"):
            nf = "YES"
        conf = float(result.get("confidence") or 0.0)
        conf = max(0.0, min(1.0, conf))
        evidence = result.get("evidence") or []
        if not isinstance(evidence, list):
            evidence = [str(evidence)]
        evidence = [str(x)[:200] for x in evidence if str(x).strip()][:3]
        if not evidence:
            evidence = ["no evidence"]

        out = {"needs_follow_up": nf, "confidence": conf, "evidence": evidence}
        try:
            _ai_gate_db_put(conversation_id, last_msg_ts, out)
        except Exception:
            pass
        return out
    except Exception:
        return {"needs_follow_up": "YES", "confidence": 0.0, "evidence": ["ai exception"]}


@app.post("/jobs/verify_pending")
async def verify_pending(request: Request, limit: int = 200):
    """
    SLA verifier:
      - For each PENDING SMS issue where due_ts <= now:
          - If ANY outbound where message.dateAdded > first_inbound_ts -> RESOLVED
          - Else -> promote to OPEN
      - For each PENDING CALL issue where due_ts <= now:
          - If ANY staff outbound message/call log after created_ts -> RESOLVED
          - Else -> promote to OPEN
    """
    _auth_or_401(request)

    now_local = _now_local()
    now_iso = now_local.isoformat()

    conn = db()
    rows = conn.execute("""
        SELECT id, contact_id, phone, conversation_id, first_inbound_ts, due_ts, outbound_count
        FROM issues
        WHERE status='PENDING'
          AND issue_type='SMS'
          AND due_ts <= ?
        ORDER BY due_ts ASC
        LIMIT ?
    """, (now_iso, limit)).fetchall()
    call_rows = conn.execute("""
        SELECT id, conversation_id, contact_id, phone, created_ts, due_ts, outbound_count
        FROM issues
        WHERE status='PENDING'
          AND issue_type='CALL'
          AND due_ts <= ?
        ORDER BY due_ts ASC
        LIMIT ?
    """, (now_iso, limit)).fetchall()
    conn.close()

    checked = 0
    promoted = 0
    auto_resolved = 0
    updated_counts = 0
    errors = 0
    call_checked = 0
    call_promoted = 0
    call_auto_resolved = 0
    call_updated_counts = 0
    call_errors = 0
    ai_checked = 0
    ai_suppressed = 0
    ai_skipped_budget = 0
    ai_run_started = dt.datetime.now(tz=dt.timezone.utc)

    for r in rows:
        checked += 1
        issue_id = r["id"]
        contact_id = r["contact_id"]
        phone = r["phone"]
        conv_id = r["conversation_id"]
        if not conv_id:
            try:
                conv_id = await ghl_find_conversation_id_for_contact(contact_id, phone)
            except Exception:
                conv_id = None

        if not conv_id:
            conn2 = db()
            conn2.execute("""
                UPDATE issues
                SET status='OPEN'
                WHERE id=? AND status='PENDING'
            """, (issue_id,))
            conn2.commit()
            conn2.close()
            promoted += 1
            _flow_log(
                "sms.promoted_open",
                issue_id=issue_id,
                contact_id=contact_id,
                conversation_id=None,
                via="verify_pending_no_conversation",
            )
            continue

        try:
            msgs = await ghl_list_messages(conv_id, limit=50)
        except HTTPException:
            errors += 1
            continue

        try:
            fi = dt.datetime.fromisoformat((r["first_inbound_ts"] or "").replace("Z", "+00:00"))
        except Exception:
            fi = None

        outbound_after = False
        out_count = 0
        latest_staff_ts: Optional[dt.datetime] = None
        latest_staff_uid: Optional[str] = None

        for m in msgs:
            if _msg_is_staff_outbound(m):
                out_count += 1

                mts0 = _msg_ts(m)
                if mts0 is not None:
                    if latest_staff_ts is None or mts0 > latest_staff_ts:
                        latest_staff_ts = mts0
                        latest_staff_uid = str(m.get("userId") or "")

                if fi is not None:
                    mts = _msg_ts(m)
                    if mts is None:
                        continue
                    try:
                        fi_utc = fi.astimezone(dt.timezone.utc) if fi.tzinfo else fi.replace(tzinfo=ZoneInfo(TZ_NAME)).astimezone(dt.timezone.utc)
                        if mts.astimezone(dt.timezone.utc) > fi_utc:
                            outbound_after = True
                    except Exception:
                        pass
            else:
                direction = _msg_direction(m)
                if direction == "inbound":
                    mts_in = _msg_ts(m)
                    if mts_in is not None and (
                        latest_customer_inbound_ts is None or mts_in > latest_customer_inbound_ts
                    ):
                        latest_customer_inbound_ts = mts_in
                        latest_customer_inbound_text = _msg_text(m)

        ack_closeout_after_staff = False
        if (
            ACK_CLOSE_ENABLED
            and latest_staff_ts is not None
            and latest_customer_inbound_ts is not None
            and latest_customer_inbound_ts > latest_staff_ts
            and _is_ack_closeout(latest_customer_inbound_text)
        ):
            try:
                staff_local = latest_staff_ts.astimezone(ZoneInfo(TZ_NAME))
                inbound_local = latest_customer_inbound_ts.astimezone(ZoneInfo(TZ_NAME))
                if ACK_CLOSE_WINDOW_MODE == "eod":
                    window_end = _business_day_end_for(staff_local)
                    ack_closeout_after_staff = (inbound_local >= staff_local) and (inbound_local <= window_end)
                else:
                    delta = inbound_local - staff_local
                    ack_closeout_after_staff = 0 <= delta.total_seconds() <= (ACK_CLOSE_WINDOW_HOURS * 3600.0)
            except Exception:
                ack_closeout_after_staff = False
        # AI follow-up gate (optional): run only if deterministic checks did not already resolve
        ai_suppress = False
        ai_gate = None
        if not (outbound_after or ack_closeout_after_staff):
            elapsed = (dt.datetime.now(tz=dt.timezone.utc) - ai_run_started).total_seconds()
            if ai_checked >= AI_GATE_MAX_ISSUES_PER_RUN or elapsed >= AI_GATE_RUN_BUDGET_SECONDS:
                ai_skipped_budget += 1
            else:
                ai_checked += 1
                try:
                    ai_gate = await ai_gate_classify(conv_id, msgs)
                    if (
                        ai_gate.get("needs_follow_up") == "NO"
                        and float(ai_gate.get("confidence") or 0.0) >= AI_GATE_SUPPRESS_NO_CONFIDENCE
                    ):
                        ai_suppress = True
                        ai_suppressed += 1
                except Exception:
                    ai_suppress = False

        if ai_gate is not None:
            try:
                _flow_log(
                    "ai_gate.decision",
                    issue_id=issue_id,
                    conversation_id=conv_id,
                    needs_follow_up=str(ai_gate.get("needs_follow_up")),
                    confidence=float(ai_gate.get("confidence") or 0.0),
                    suppressed=bool(ai_suppress),
                )
            except Exception:
                pass
        conn2 = db()
        if conv_id != r["conversation_id"]:
            conn2.execute("UPDATE issues SET conversation_id=? WHERE id=?", (conv_id, issue_id))
            conn2.commit()

        prev_out = r["outbound_count"] if r["outbound_count"] is not None else 0
        if out_count != prev_out:
            conn2.execute("UPDATE issues SET outbound_count=? WHERE id=?", (out_count, issue_id))
            conn2.commit()
            updated_counts += 1

        if outbound_after or ack_closeout_after_staff or ai_suppress:
            conn2.execute("""
                UPDATE issues
                SET status='RESOLVED', resolved_ts=?
                WHERE id=? AND status='PENDING'
            """, (now_iso, issue_id))
            conn2.commit()
            auto_resolved += 1
            if ai_suppress and ai_gate is not None:
                try:
                    _update_issue_meta(
                        issue_id,
                        {
                            "ai_gate_needs_follow_up": str(ai_gate.get("needs_follow_up")),
                            "ai_gate_confidence": float(ai_gate.get("confidence") or 0.0),
                            "ai_gate_evidence": ai_gate.get("evidence") or [],
                            "ai_gate_model": AI_GATE_MODEL,
                            "ai_gate_ts": now_iso,
                        },
                    )
                except Exception:
                    pass
            _flow_log(
                "sms.auto_resolved",
                issue_id=issue_id,
                conversation_id=conv_id,
                via=("verify_pending_ai_gate" if ai_suppress else ("verify_pending_ack_closeout" if ack_closeout_after_staff else "verify_pending")),
            )
            conn2.close()
            continue

        conn2.execute("""
            UPDATE issues
            SET status='OPEN'
            WHERE id=? AND status='PENDING'
        """, (issue_id,))
        conn2.commit()
        promoted += 1
        _flow_log(
            "sms.promoted_open",
            issue_id=issue_id,
            conversation_id=conv_id,
            via="verify_pending",
        )
        conn2.close()

    for r in call_rows:
        call_checked += 1
        issue_id = r["id"]
        conv_id = r["conversation_id"]
        contact_id = r["contact_id"]
        phone = r["phone"]

        if not conv_id:
            try:
                conv_id = await ghl_find_conversation_id_for_contact(contact_id, phone)
            except Exception:
                conv_id = None

        msgs: List[Dict[str, Any]] = []
        if conv_id:
            try:
                msgs = await ghl_list_messages(conv_id, limit=50)
            except HTTPException:
                call_errors += 1
                continue

        created = _parse_iso_dt(r["created_ts"])
        created_utc: Optional[dt.datetime] = None
        if created is not None:
            try:
                created_utc = created.astimezone(dt.timezone.utc) if created.tzinfo else created.replace(
                    tzinfo=ZoneInfo(TZ_NAME)
                ).astimezone(dt.timezone.utc)
            except Exception:
                created_utc = None

        outbound_after = False
        out_count = 0
        latest_staff_ts: Optional[dt.datetime] = None
        latest_staff_uid: Optional[str] = None

        for m in msgs:
            if _msg_is_staff_outbound(m):
                out_count += 1
                if created_utc is None:
                    continue
                mts = _msg_ts(m)
                if mts is None:
                    continue
                try:
                    if mts.astimezone(dt.timezone.utc) > created_utc:
                        outbound_after = True
                except Exception:
                    pass

        if conv_id and latest_staff_ts is not None:
            try:
                set_last_internal_outbound(
                    conv_id,
                    latest_staff_ts.astimezone(ZoneInfo(TZ_NAME)).isoformat(),
                    latest_staff_uid or None,
                )
            except Exception:
                pass

        conn2 = db()
        if conv_id and conv_id != r["conversation_id"]:
            conn2.execute("UPDATE issues SET conversation_id=? WHERE id=?", (conv_id, issue_id))
            conn2.commit()

        prev_out = r["outbound_count"] if r["outbound_count"] is not None else 0
        if out_count != prev_out:
            conn2.execute("UPDATE issues SET outbound_count=? WHERE id=?", (out_count, issue_id))
            conn2.commit()
            call_updated_counts += 1

        if outbound_after:
            conn2.execute("""
                UPDATE issues
                SET status='RESOLVED', resolved_ts=?
                WHERE id=? AND status='PENDING'
            """, (now_iso, issue_id))
            conn2.commit()
            call_auto_resolved += 1
            _flow_log(
                "call.auto_resolved",
                issue_id=issue_id,
                contact_id=contact_id,
                conversation_id=conv_id,
                via="verify_pending",
            )
            conn2.close()
            continue

        conn2.execute("""
            UPDATE issues
            SET status='OPEN'
            WHERE id=? AND status='PENDING'
        """, (issue_id,))
        conn2.commit()
        call_promoted += 1
        _flow_log(
            "call.promoted_open",
            issue_id=issue_id,
            contact_id=contact_id,
            conversation_id=conv_id,
            via="verify_pending",
        )
        conn2.close()

    return {
        "job": "verify_pending",
        "checked": checked,
        "promoted_open": promoted,
        "auto_resolved": auto_resolved,
        "updated_counts": updated_counts,
        "errors": errors,
        "call_checked": call_checked,
        "call_promoted_open": call_promoted,
        "call_auto_resolved": call_auto_resolved,
        "call_updated_counts": call_updated_counts,
        "call_errors": call_errors,
        "ai_checked": ai_checked,
        "ai_suppressed": ai_suppressed,
        "ai_skipped_budget": ai_skipped_budget,
    }


# ==========================
# Summary logic (Managers only, v1)
# ==========================
def _short_phone(p: Optional[str]) -> str:
    if not p:
        return "-"
    s = re.sub(r"\D", "", p)
    if len(s) >= 10:
        return f"+1***{s[-4:]}"
    return p

def _parse_iso(ts: Optional[str]) -> Optional[dt.datetime]:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None

def _is_escalated(issue_type: str, first_inbound_ts: Optional[str], created_ts: str, now_local: dt.datetime) -> bool:
    """
    Escalation: still OPEN after 24 business hours.
    Uses business-hours adder from first_inbound_ts (SMS) or created_ts (CALL).
    """
    base_ts = first_inbound_ts if issue_type == "SMS" and first_inbound_ts else created_ts
    base = _parse_iso(base_ts)
    if not base:
        return False
    if base.tzinfo is None:
        base = base.replace(tzinfo=ZoneInfo(TZ_NAME))
    threshold = add_business_hours(base.astimezone(ZoneInfo(TZ_NAME)), 24.0)
    return now_local >= threshold

async def _manager_conversation_for_contact(contact_id: str) -> Optional[str]:
    # lookup via conversations/search?contactId=...
    return await ghl_find_conversation_id_for_contact(contact_id, None)

def _summary_title(slot: str) -> str:
    s = slot.lower()
    if s == "morning":
        return "Morning"
    if s == "midday":
        return "Midday"
    if s == "afternoon":
        return "Afternoon"
    return slot.capitalize()

def _fmt_dt_local(ts: Optional[str]) -> str:
    d = _parse_iso(ts)
    if not d:
        return "-"
    if d.tzinfo is None:
        d = d.replace(tzinfo=ZoneInfo(TZ_NAME))
    loc = d.astimezone(ZoneInfo(TZ_NAME))
    return loc.strftime("%-I:%M%p").lower()

def _build_section_lines(rows: List[sqlite3.Row], label: str, now_local: dt.datetime) -> Tuple[List[str], List[str]]:
    """
    Returns (normal_lines, escalated_lines)
    """
    normal: List[str] = []
    escalated: List[str] = []

    for r in rows[:SUMMARY_MAX_ITEMS_PER_SECTION]:
        it = r["issue_type"]
        who = _display_name(r)
        last_in = r["last_inbound_ts"] or r["created_ts"]
        due = r["due_ts"]
        inc = r["inbound_count"] if r["inbound_count"] is not None else 0
        marker = f"#{r['id']} {who} — {_fmt_dt_local(last_in)} | due {_fmt_dt_local(due)}"
        if it == "SMS":
            marker += f" in={inc}"
        if _is_escalated(it, r["first_inbound_ts"], r["created_ts"], now_local):
            escalated.append(marker)
        else:
            normal.append(marker)

    header = f"{label} ({len(rows)})"
    if not rows:
        return [f"{header}: none"], []
    return [header + ":"] + normal, escalated

def _display_name(r: sqlite3.Row) -> str:
    try:
        meta = json.loads(r["meta"] or "{}")
    except Exception:
        meta = {}
    name = (meta.get("contact_name") or "").strip()
    return name if name else _short_phone(r["phone"])

async def _enrich_issues_with_contact_names(issues: List[sqlite3.Row]) -> None:
    """
    For issues missing contact_name in meta, fetch from GHL API and update DB.
    """
    conn = db()
    for issue in issues:
        try:
            meta = json.loads(issue["meta"] or "{}")
        except Exception:
            meta = {}
        
        # Skip if contact_name already exists
        if (meta.get("contact_name") or "").strip():
            continue
        
        # Skip if no contact_id to look up
        contact_id = issue["contact_id"]
        if not contact_id:
            continue
        
        # Fetch contact name from GHL API
        try:
            contact_name = await ghl_get_contact_name(contact_id)
            if contact_name:
                meta["contact_name"] = contact_name
                conn.execute(
                    "UPDATE issues SET meta=? WHERE id=?",
                    (json.dumps(meta), issue["id"])
                )
                conn.commit()
        except Exception:
            pass
    
    conn.close()


@app.post("/jobs/send_summary")
async def send_summary(request: Request, slot: str = "morning", dry_run: int = 0):
    """
    Manager-only scheduled summaries at 8/11/3.

    Sections:
      - Missed / Unanswered Calls
      - Unanswered Customer Texts
      - Resolved since last summary (dopamine, then disappears)

    Escalation:
      - If still OPEN after 24 business hours -> Escalated section
    """
    _auth_or_401(request)

    now_local = _now_local()
    now_iso = now_local.isoformat()

    # (Optional) run resolver first so summaries don't include already-answered threads
    # Keep deterministic, but don't fail summary if resolver has transient API issue.
    try:
        await poll_resolver(request, limit=500)
    except Exception:
        pass

    conn = db()

    # Overdue = OPEN and now >= due_ts
    overdue_sms = conn.execute("""
      SELECT *
      FROM issues
      WHERE status='OPEN' AND issue_type='SMS' AND due_ts <= ?
      ORDER BY due_ts ASC
    """, (now_iso,)).fetchall()

    overdue_calls = conn.execute("""
      SELECT *
      FROM issues
      WHERE status='OPEN' AND issue_type='CALL' AND due_ts <= ?
      ORDER BY due_ts ASC
    """, (now_iso,)).fetchall()

    # Resolved since last summary
    key = "last_summary_ts"
    slot_key = f"last_summary_ts_{slot.lower()}"  # backward-compat fallback
    last_ts = kv_get(key) or kv_get(slot_key)
    resolved_since: List[sqlite3.Row] = []
    if last_ts:
        resolved_since = conn.execute("""
          SELECT *
          FROM issues
          WHERE status='RESOLVED'
            AND resolved_ts IS NOT NULL
            AND resolved_ts > ?
            AND resolved_ts <= ?
          ORDER BY resolved_ts DESC
          LIMIT 100
        """, (last_ts, now_iso)).fetchall()

    conn.close()

    # Enrich issues with contact names if missing
    await _enrich_issues_with_contact_names(list(overdue_sms) + list(overdue_calls) + list(resolved_since))

    # Re-fetch issues after enrichment to get updated meta data
    conn = db()
    overdue_sms = conn.execute("""
      SELECT *
      FROM issues
      WHERE status='OPEN' AND issue_type='SMS' AND due_ts <= ?
      ORDER BY due_ts ASC
    """, (now_iso,)).fetchall()

    overdue_calls = conn.execute("""
      SELECT *
      FROM issues
      WHERE status='OPEN' AND issue_type='CALL' AND due_ts <= ?
      ORDER BY due_ts ASC
    """, (now_iso,)).fetchall()

    if last_ts:
        resolved_since = conn.execute("""
          SELECT *
          FROM issues
          WHERE status='RESOLVED'
            AND resolved_ts IS NOT NULL
            AND resolved_ts > ?
            AND resolved_ts <= ?
          ORDER BY resolved_ts DESC
          LIMIT 100
        """, (last_ts, now_iso)).fetchall()

    conn.close()

    title = _summary_title(slot)
    lines: List[str] = []
    lines.append(f"NTPP Sentinel — {title} ({_fmt_date_local(now_local)}) • as of {_fmt_as_of_local(now_local)}")
    lines.append(f"Overdue: Calls {len(overdue_calls)} | Texts {len(overdue_sms)}")
    lines.append("")

    # Calls
    sec_calls, esc_calls = _build_section_lines(overdue_calls, "Calls", now_local)
    lines.extend(sec_calls)

    # SMS
    sec_sms, esc_sms = _build_section_lines(overdue_sms, "Texts", now_local)
    lines.extend(sec_sms)

    # Escalations section (manager-only rollup)
    escalated_lines = []
    if esc_calls or esc_sms:
        escalated_lines.append("⚠️ Escalated (24+ business hrs):")
        escalated_lines.extend(esc_calls[:SUMMARY_MAX_ITEMS_PER_SECTION])
        escalated_lines.extend(esc_sms[:SUMMARY_MAX_ITEMS_PER_SECTION])

    if escalated_lines:
        lines.extend(escalated_lines)

    # Dopamine section: show once then disappears
    if last_ts:
        if resolved_since:
            lines.append(f"✅ Resolved since last summary ({len(resolved_since)}):")
            for r in resolved_since[:RESOLVED_SINCE_MAX_ITEMS]:
                who = _display_name(r)
                rt = _fmt_dt_local(r["resolved_ts"])
                lines.append(f"#{r['id']} {r['issue_type']} {who} at {rt}")
        else:
            lines.append("✅ Resolved since last summary: none")
    lines.append("")
    lines.append("Reply:")
    lines.append("Open 3 | Resolve 3 5 6 | Spam 7 | Note 3 <text> | List | More")

    # keep SMS concise
    body = "\n".join(lines)
    if len(body) > 1450:
        body = body[:1450] + "\n…"

    # Update last_summary_ts for this slot (even in dry_run, so set after send unless dry_run)
    result = {
        "job": "send_summary",
        "slot": slot,
        "overdue_sms": len(overdue_sms),
        "overdue_calls": len(overdue_calls),
        "resolved_since": len(resolved_since),
        "dry_run": bool(dry_run),
        "body": body,
    }

    if dry_run:
        return result

    if not MANAGER_CONTACT_IDS:
        result["sent"] = False
        result["error"] = "MANAGER_CONTACT_IDS not configured"
        return result

    sent_to: List[str] = []
    errors: List[str] = []

    for mgr_contact_id in MANAGER_CONTACT_IDS:
        try:
            conv_id = await _manager_conversation_for_contact(mgr_contact_id)
            if not conv_id:
                errors.append(f"manager contact {mgr_contact_id}: no conversation found")
                continue
            await ghl_send_message(conv_id, mgr_contact_id, body)
            sent_to.append(mgr_contact_id)
        except Exception as e:
            errors.append(f"manager contact {mgr_contact_id}: {type(e).__name__}")

    kv_set(key, now_iso)
    kv_set(slot_key, now_iso)

    result["sent"] = True if sent_to else False
    result["sent_to"] = sent_to
    if errors:
        result["errors"] = errors
    return result


# ==========================
# Escalations job (optional separate rollup; v1 placeholder)
# ==========================
@app.post("/jobs/escalations")
async def escalations(request: Request, dry_run: int = 0, limit: int = 200):
    _auth_or_401(request)

    now_local = _now_local()
    now_iso = now_local.isoformat()

    # Keep deterministic and reduce false positives from stale issue states.
    try:
        await poll_resolver(request, limit=500)
    except Exception:
        pass
    try:
        await verify_pending(request, limit=500)
    except Exception:
        pass

    conn = db()
    rows = conn.execute("""
      SELECT *
      FROM issues
      WHERE status='OPEN'
        AND due_ts <= ?
        AND breach_notified_ts IS NULL
      ORDER BY due_ts ASC
      LIMIT ?
    """, (now_iso, limit)).fetchall()
    conn.close()
    if not rows:
        return {
            "job": "escalations",
            "new_breaches": 0,
            "dry_run": bool(dry_run),
            "sent": False,
        }

    await _enrich_issues_with_contact_names(list(rows))

    conn = db()
    rows = conn.execute("""
      SELECT *
      FROM issues
      WHERE status='OPEN'
        AND due_ts <= ?
        AND breach_notified_ts IS NULL
      ORDER BY due_ts ASC
      LIMIT ?
    """, (now_iso, limit)).fetchall()
    conn.close()
    if not rows:
        return {
            "job": "escalations",
            "new_breaches": 0,
            "dry_run": bool(dry_run),
            "sent": False,
        }

    lines: List[str] = []
    lines.append(f"NTPP Sentinel — SLA Breach Alert ({_fmt_date_local(now_local)}) • as of {_fmt_as_of_local(now_local)}")
    lines.append(f"New breaches: {len(rows)}")
    lines.append("")

    calls = [r for r in rows if (r["issue_type"] or "").upper() == "CALL"]
    texts = [r for r in rows if (r["issue_type"] or "").upper() == "SMS"]

    if calls:
        lines.append(f"Calls ({len(calls)}):")
        for r in calls[:SUMMARY_MAX_ITEMS_PER_SECTION]:
            lines.append(f"#{r['id']} {_display_name(r)} — due {_fmt_dt_local(r['due_ts'])}")

    if texts:
        lines.append(f"Texts ({len(texts)}):")
        for r in texts[:SUMMARY_MAX_ITEMS_PER_SECTION]:
            inc = r["inbound_count"] if r["inbound_count"] is not None else 0
            lines.append(f"#{r['id']} {_display_name(r)} — due {_fmt_dt_local(r['due_ts'])} in={inc}")

    shown = min(len(calls), SUMMARY_MAX_ITEMS_PER_SECTION) + min(len(texts), SUMMARY_MAX_ITEMS_PER_SECTION)
    if len(rows) > shown:
        lines.append(f"+{len(rows) - shown} more")

    body = "\n".join(lines)
    if len(body) > 1450:
        body = body[:1450] + "\n…"

    result = {
        "job": "escalations",
        "new_breaches": len(rows),
        "dry_run": bool(dry_run),
        "body": body,
    }

    if dry_run:
        return result

    if not MANAGER_CONTACT_IDS:
        result["sent"] = False
        result["error"] = "MANAGER_CONTACT_IDS not configured"
        return result

    sent_to: List[str] = []
    errors: List[str] = []

    for mgr_contact_id in MANAGER_CONTACT_IDS:
        try:
            conv_id = await _manager_conversation_for_contact(mgr_contact_id)
            if not conv_id:
                errors.append(f"manager contact {mgr_contact_id}: no conversation found")
                continue
            await ghl_send_message(conv_id, mgr_contact_id, body)
            sent_to.append(mgr_contact_id)
        except Exception as e:
            errors.append(f"manager contact {mgr_contact_id}: {type(e).__name__}")

    # Mark alerted only if at least one manager received the alert.
    if sent_to:
        conn = db()
        ids = [r["id"] for r in rows]
        q = "UPDATE issues SET breach_notified_ts=? WHERE id IN (%s) AND breach_notified_ts IS NULL" % ",".join(["?"] * len(ids))
        conn.execute(q, [now_iso] + ids)
        conn.commit()
        conn.close()
        _flow_log("escalations.sent", issue_ids=ids, sent_to_count=len(sent_to))

    result["sent"] = True if sent_to else False
    result["sent_to"] = sent_to
    if errors:
        result["errors"] = errors
    result["marked_notified"] = len(rows) if sent_to else 0
    return result
