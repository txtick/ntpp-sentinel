from fastapi import FastAPI, Request, HTTPException
import os, json, sqlite3, datetime as dt
from typing import Any, Dict, Optional, List, Tuple
import httpx
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

# GoHighLevel / LeadConnector API
GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")
GHL_TOKEN = os.getenv("GHL_TOKEN", "")  # Private Integration token (Bearer)
GHL_VERSION = os.getenv("GHL_VERSION", "2021-07-28")

# Summary recipients (managers only, v1)
MANAGER_CONTACT_IDS = [
    s.strip() for s in (os.getenv("MANAGER_CONTACT_IDS", "")).split(",") if s.strip()
]

# Limits to keep SMS short and low-noise
SUMMARY_MAX_ITEMS_PER_SECTION = int(os.getenv("SUMMARY_MAX_ITEMS_PER_SECTION", "8"))

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

    conn.commit()
    conn.close()

@app.on_event("startup")
def _startup():
    os.makedirs("/data", exist_ok=True)
    init_db()

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

def _is_business_time(ts: dt.datetime) -> bool:
    # Mon-Fri 09:00-18:00 local
    if ts.weekday() >= 5:
        return False
    start = ts.replace(hour=9, minute=0, second=0, microsecond=0)
    end = ts.replace(hour=18, minute=0, second=0, microsecond=0)
    return start <= ts <= end

def _roll_to_next_business_open(ts: dt.datetime) -> dt.datetime:
    cur = ts
    while True:
        if cur.weekday() >= 5:
            days_ahead = 7 - cur.weekday()
            cur = (cur + dt.timedelta(days=days_ahead)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue
        if cur.hour < 9:
            return cur.replace(hour=9, minute=0, second=0, microsecond=0)
        if cur.hour >= 18:
            cur = (cur + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue
        return cur

def add_business_hours(start_local: dt.datetime, hours: float) -> dt.datetime:
    """
    Deterministic business-hours adder: Mon–Fri 09:00–18:00 local.
    Adds hours strictly across business windows.
    """
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=ZoneInfo(TZ_NAME))

    remaining = hours * 3600.0
    cur = _roll_to_next_business_open(start_local)

    while remaining > 0:
        day_end = cur.replace(hour=18, minute=0, second=0, microsecond=0)
        available = (day_end - cur).total_seconds()
        if remaining <= available:
            return cur + dt.timedelta(seconds=remaining)

        remaining -= available
        cur = (cur + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        while cur.weekday() >= 5:
            cur = (cur + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

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

def _list_open_issues_compact(limit: int, offset: int = 0) -> tuple[list[dict], int]:
    """
    Returns (issues, total_open).
    Uses your existing DB access patterns. This assumes you already have a way
    to fetch OPEN issues ordered by due/age, like the summary does.
    """
    # If you already have a function used by send_summary to get OPEN items,
    # reuse it here to guarantee identical ordering.
    issues = get_open_issues_ordered()  # <-- if this exists in your file, use it
    # If you DON'T have that function, tell me what you have (or I’ll adapt it).
    total = len(issues)
    return issues[offset: offset + limit], total

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

def _fmt_hhmm_ampm(dt_str: str) -> str:
    """
    Convert stored ISO-ish timestamp string to 'h:mmap' like the summary.
    If parsing fails, return '?'.
    """
    if not dt_str:
        return "?"
    try:
        # Your DB stores ISO strings (with timezone). datetime.fromisoformat can parse most of these.
        dt = datetime.fromisoformat(dt_str)
        try:
            return dt.strftime("%-I:%M%p").lower()
        except Exception:
            return dt.strftime("%I:%M%p").lstrip("0").lower()
    except Exception:
        return "?"

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
        SELECT id, issue_type, phone, contact_id, created_ts, due_ts, inbound_count, last_inbound_ts
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
        who = _mask_phone(phone)

        last_s = _fmt_hhmm_ampm(r.get("last_inbound_ts") or "")
        due_s = _fmt_hhmm_ampm(r.get("due_ts") or "")

        line = f"#{iid} {who} — {last_s} | due {due_s}"
        if it == "SMS":
            line += f" in={r.get('inbound_count', 0)}"
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

    return "\n".join(lines)

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
      - Internal + "SENTINEL " -> command
      - Internal + not command -> ignore
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

    contact_name = _extract_contact_name(payload)
    if not contact_name:
        try:
            contact_name = await ghl_get_contact_name(contact_id)
        except Exception:
            contact_name = None
    direction = _extract_direction(payload)
    contact_type = _extract_contact_type(payload)

    contact_name = _extract_contact_name(payload)
    if not contact_name:
        try:
            contact_name = await ghl_get_contact_name(contact_id)
        except Exception:
            contact_name = None

    if direction in ("outbound", "outgoing"):
        return {"received": True, "ignored": "outbound"}

    # Internal manager commands (no "SENTINEL" prefix required; handled by handle_command)
    if contact_type == "internal":
        result = await handle_command(text=text, command_contact_id=contact_id, command_from_phone=from_phone)

        # Only respond if it was actually a recognized command
        if result.get("ok") and result.get("text") and contact_id:
            try:
                conv_id = await _manager_conversation_for_contact(contact_id)
                if conv_id:
                    await ghl_send_message(conv_id, contact_id, result["text"])
            except Exception:
                pass
            return {"received": True, "command": True, "result": result}

        # internal non-command
        return {"received": True, "ignored": "internal_non_command"}

    now_local = _now_local()
    created_ts = now_local.isoformat()
    due_ts = add_business_hours(now_local, 2.0).isoformat()

    conversation_id: Optional[str] = None
    try:
        conversation_id = await ghl_find_conversation_id_for_contact(contact_id, from_phone)
    except Exception:
        conversation_id = None

    conn = db()

    row = None
    if conversation_id:
        row = conn.execute(
            "SELECT * FROM issues WHERE status='OPEN' AND issue_type='SMS' AND conversation_id=? ORDER BY id DESC LIMIT 1",
            (conversation_id,)
        ).fetchone()

    if row is None and from_phone:
        row = conn.execute(
            "SELECT * FROM issues WHERE status='OPEN' AND issue_type='SMS' AND phone=? ORDER BY id DESC LIMIT 1",
            (from_phone,)
        ).fetchone()

    if row is None:
        meta = {
            "last_text": text[:500],
            "source": "inbound_sms_webhook",
        }
        if contact_name:
            meta["contact_name"] = contact_name
        conn.execute("""
            INSERT INTO issues
              (issue_type, contact_id, phone, created_ts, due_ts, status, meta,
               first_inbound_ts, last_inbound_ts, inbound_count, outbound_count, conversation_id)
            VALUES
              ('SMS', ?, ?, ?, ?, 'OPEN', ?, ?, ?, 1, 0, ?)
        """, (
            contact_id, from_phone, created_ts, due_ts, json.dumps(meta),
            created_ts, created_ts, conversation_id
        ))
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
                meta=?
            WHERE id=?
        """, (created_ts, contact_id, from_phone, conversation_id, json.dumps(meta), row["id"]))

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

    contact_name = _extract_contact_name(payload)
    if not contact_name:
        try:
            contact_name = await ghl_get_contact_name(contact_id)
        except Exception:
            contact_name = None

    conn = db()
    if _is_spam(conn, from_phone):
        conn.close()
        return {"received": True, "ignored": "spam_phone"}

    now_local = _now_local()
    created_ts = now_local.isoformat()
    due_ts = add_business_hours(now_local, 2.0).isoformat()

    meta = {"source": "voicemail_route=tech_sentinel"}
    if contact_name:
        meta["contact_name"] = contact_name
    conn.execute("""
        INSERT INTO issues (issue_type, contact_id, phone, created_ts, due_ts, status, meta)
        VALUES ('CALL', ?, ?, ?, ?, 'OPEN', ?)
    """, (contact_id, from_phone, created_ts, due_ts, json.dumps(meta)))
    conn.commit()
    conn.close()
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
        WHERE status='OPEN'
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

        for m in msgs:
            d = _msg_direction(m)
            if d == "outbound":
                out_count += 1

            if fi is not None and d == "outbound":
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
                WHERE id=? AND status='OPEN'
            """, (now, issue_id))
            conn2.commit()
            resolved += 1

        conn2.close()

    return {"job": "poll_resolver", "checked": checked, "resolved": resolved, "updated_counts": updated_counts}


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
    key = f"last_summary_ts_{slot.lower()}"
    last_ts = kv_get(key)
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
          LIMIT 25
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
          LIMIT 25
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
            for r in resolved_since[:SUMMARY_MAX_ITEMS_PER_SECTION]:
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

    result["sent"] = True if sent_to else False
    result["sent_to"] = sent_to
    if errors:
        result["errors"] = errors
    return result


# ==========================
# Escalations job (optional separate rollup; v1 placeholder)
# ==========================
@app.post("/jobs/escalations")
async def escalations(request: Request):
    _auth_or_401(request)
    return {"job": "escalations", "status": "placeholder"}
