import datetime as dt
import json
import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional, Tuple

from db import db
from handlers.sms import normalize_phone


def kv_get(key: str) -> Optional[str]:
    conn = db()
    row = conn.execute("SELECT value FROM kv_store WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    conn = db()
    conn.execute(
        "INSERT INTO kv_store(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


def update_issue_meta(issue_id: int, updates: Dict[str, Any]) -> None:
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


def set_resolved_metadata(
    issue_id: int,
    resolved_by: str,
    now_local: Callable[[], dt.datetime],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    payload: Dict[str, Any] = {
        "resolved_by": resolved_by,
        "resolved_meta_ts": now_local().isoformat(),
    }
    if extra:
        payload.update(extra)
    update_issue_meta(issue_id, payload)


def get_issue_by_id(issue_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    conn.close()
    return row


def add_note(issue_id: int, note: str, now_local: Callable[[], dt.datetime]) -> bool:
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
    notes.append({"ts": now_local().isoformat(), "text": note[:500]})
    meta["notes"] = notes
    conn.execute("UPDATE issues SET meta=? WHERE id=?", (json.dumps(meta), issue_id))
    conn.commit()
    conn.close()
    return True


def mask_phone(phone: str) -> str:
    p = (phone or "").strip()
    if p.startswith("+1") and len(p) >= 12:
        return "+1***" + p[-4:]
    if len(p) >= 4:
        return "***" + p[-4:]
    return p or "Unknown"


def fmt_hhmm_ampm(value) -> str:
    if not value:
        return "?"

    parsed = None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        s = str(value).strip()
        if not s:
            return "?"
        try:
            parsed = dt.datetime.fromisoformat(s)
        except Exception:
            try:
                parsed = dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    parsed = dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
                except Exception:
                    return "?"

    try:
        return parsed.strftime("%-I:%M%p").lower()
    except Exception:
        return parsed.strftime("%I:%M%p").lstrip("0").lower()


def list_open_issues(limit: int = 20, offset: int = 0) -> Tuple[List[dict], int]:
    conn = db()
    total = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM issues
        WHERE status='OPEN'
        """
    ).fetchone()["n"]

    rows = conn.execute(
        """
        SELECT id, issue_type, phone, contact_id, contact_name, created_ts, due_ts, inbound_count, last_inbound_ts
        FROM issues
        WHERE status='OPEN'
        ORDER BY due_ts ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()

    out: List[dict] = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "issue_type": row["issue_type"],
                "phone": row["phone"],
                "contact_id": row["contact_id"],
                "contact_name": row["contact_name"],
                "created_ts": row["created_ts"],
                "due_ts": row["due_ts"],
                "inbound_count": row["inbound_count"] if row["inbound_count"] is not None else 0,
                "last_inbound_ts": row["last_inbound_ts"] or row["created_ts"],
            }
        )
    return out, int(total)


def render_list_like_summary(rows: List[dict], total_open: int, offset: int, limit: int) -> str:
    calls: List[str] = []
    texts: List[str] = []

    for row in rows:
        issue_id = row["id"]
        issue_type = (row.get("issue_type") or "").upper()
        phone = row.get("phone") or ""
        name = (row.get("contact_name") or "").strip()
        who = name if name else mask_phone(phone)

        last_s = fmt_hhmm_ampm(row.get("last_inbound_ts") or "")
        due_s = fmt_hhmm_ampm(row.get("due_ts") or "")

        line = f"#{issue_id} {who} — {last_s} | due {due_s}"
        if issue_type == "SMS":
            count = int(row.get("inbound_count", 0) or 0)
            if count > 1:
                line += f" ({count})"
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
    lines.append("Reply: More" if end < total_open else "End of list. Reply: List")
    return "\n".join(lines)


def set_issue_contact_name(issue_id: int, name: str) -> None:
    if not name:
        return
    conn = db()
    conn.execute(
        "UPDATE issues SET contact_name=? WHERE id=? AND (contact_name IS NULL OR contact_name='')",
        (name, issue_id),
    )
    conn.commit()
    conn.close()


def resolve_by_id(issue_id: int, status: str, now_local: Callable[[], dt.datetime]) -> int:
    conn = db()
    now_iso = now_local().isoformat()
    cur = conn.execute(
        "UPDATE issues SET status=?, resolved_ts=? WHERE status='OPEN' AND id=?",
        (status, now_iso, issue_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount


def resolve_by_phone(
    phone: str,
    status: str,
    now_local: Callable[[], dt.datetime],
) -> Tuple[int, List[int]]:
    conn = db()
    now_iso = now_local().isoformat()
    ids = [r["id"] for r in conn.execute("SELECT id FROM issues WHERE status='OPEN' AND phone=?", (phone,)).fetchall()]
    cur = conn.execute(
        """
        UPDATE issues
        SET status=?, resolved_ts=?
        WHERE status='OPEN' AND phone=?
        """,
        (status, now_iso, phone),
    )
    conn.commit()
    conn.close()
    return cur.rowcount, ids


def resolve_by_contact_id(
    contact_id: str,
    status: str,
    now_local: Callable[[], dt.datetime],
) -> Tuple[int, List[int]]:
    conn = db()
    now_iso = now_local().isoformat()
    ids = [
        r["id"]
        for r in conn.execute("SELECT id FROM issues WHERE status='OPEN' AND contact_id=?", (contact_id,)).fetchall()
    ]
    cur = conn.execute(
        """
        UPDATE issues
        SET status=?, resolved_ts=?
        WHERE status='OPEN' AND contact_id=?
        """,
        (status, now_iso, contact_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount, ids


def resolve_by_name(
    name: str,
    status: str,
    now_local: Callable[[], dt.datetime],
) -> List[int]:
    name_l = name.strip().lower()
    if not name_l:
        return []

    conn = db()
    rows = conn.execute(
        "SELECT id FROM issues WHERE status='OPEN' AND contact_name LIKE ?",
        (f"%{name_l}%",),
    ).fetchall()
    matched_ids = [row["id"] for row in rows]

    if matched_ids:
        now_iso = now_local().isoformat()
        query = "UPDATE issues SET status=?, resolved_ts=? WHERE id IN (%s)" % ",".join(["?"] * len(matched_ids))
        conn.execute(query, [status, now_iso] + matched_ids)
        conn.commit()

    conn.close()
    return matched_ids


def looks_like_contact_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9]{10,}", value))


def resolve_target(target: str, now_local: Callable[[], dt.datetime]) -> Tuple[int, str]:
    token = target.strip()
    phone = normalize_phone(token)
    if phone:
        resolved, _ = resolve_by_phone(phone, "RESOLVED", now_local)
        return resolved, phone
    if looks_like_contact_id(token):
        resolved, _ = resolve_by_contact_id(token, "RESOLVED", now_local)
        return resolved, token
    matched_ids = resolve_by_name(token, "RESOLVED", now_local)
    return len(matched_ids), token
