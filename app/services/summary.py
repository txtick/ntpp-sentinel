import asyncio
import json
import re
import sqlite3
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from db import db
from services.business_time import BusinessTimeConfig, add_business_hours, fmt_dt_local, is_escalated


def summary_title(slot: str) -> str:
    value = slot.lower()
    if value == "morning":
        return "Morning"
    if value == "midday":
        return "Midday"
    if value == "afternoon":
        return "Afternoon"
    return slot.capitalize()


def short_phone(phone: Optional[str]) -> str:
    if not phone:
        return "-"
    digits = re.sub(r"\D", "", phone)
    if len(digits) >= 10:
        return f"+1***{digits[-4:]}"
    return phone


def display_name(row: sqlite3.Row) -> str:
    try:
        meta = json.loads(row["meta"] or "{}")
    except Exception:
        meta = {}
    name = (meta.get("contact_name") or "").strip()
    return name if name else short_phone(row["phone"])


def build_section_lines(
    rows: List[sqlite3.Row],
    label: str,
    now_local_value,
    business_time_cfg: BusinessTimeConfig,
    summary_max_items: int,
) -> Tuple[List[str], List[str]]:
    normal: List[str] = []
    escalated: List[str] = []

    for row in rows[:summary_max_items]:
        issue_type = row["issue_type"]
        who = display_name(row)
        last_in = row["last_inbound_ts"] or row["created_ts"]
        due = row["due_ts"]
        inbound_count = row["inbound_count"] if row["inbound_count"] is not None else 0
        marker = f"#{row['id']} {who} — {fmt_dt_local(business_time_cfg.tz_name, last_in)} | due {fmt_dt_local(business_time_cfg.tz_name, due)}"
        if issue_type == "SMS":
            marker += f" in={inbound_count}"
        if is_escalated(
            business_time_cfg,
            issue_type,
            row["first_inbound_ts"],
            row["created_ts"],
            now_local_value,
        ):
            escalated.append(marker)
        else:
            normal.append(marker)

    header = f"{label} ({len(rows)})"
    if not rows:
        return [f"{header}: none"], []
    return [header + ":"] + normal, escalated


async def enrich_issues_with_contact_names(
    issues: List[sqlite3.Row],
    get_contact_name: Callable[[Optional[str]], Awaitable[Optional[str]]],
) -> None:
    # Group by contact_id — one API call per unique contact, not per issue
    by_contact: Dict[str, List[tuple]] = {}
    for issue in issues:
        try:
            meta = json.loads(issue["meta"] or "{}")
        except Exception:
            meta = {}
        if (meta.get("contact_name") or "").strip():
            continue
        contact_id = issue["contact_id"]
        if not contact_id:
            continue
        by_contact.setdefault(contact_id, []).append((issue, meta))

    if not by_contact:
        return

    # One API call per unique contact_id, all in parallel
    async def _fetch_one(contact_id: str):
        try:
            return contact_id, await get_contact_name(contact_id)
        except Exception:
            return contact_id, None

    contact_names: Dict[str, str] = {
        cid: name
        for cid, name in await asyncio.gather(*[_fetch_one(cid) for cid in by_contact])
        if name
    }

    if not contact_names:
        return

    # Batch all DB writes in a single commit
    updates = []
    for contact_id, pairs in by_contact.items():
        name = contact_names.get(contact_id)
        if not name:
            continue
        for issue, meta in pairs:
            updates.append((json.dumps({**meta, "contact_name": name}), name, issue["id"]))

    conn = db()
    try:
        for meta_json, contact_name, issue_id in updates:
            conn.execute(
                "UPDATE issues SET meta=?, contact_name=COALESCE(NULLIF(contact_name,''),?) WHERE id=?",
                (meta_json, contact_name, issue_id),
            )
        conn.commit()
    finally:
        conn.close()


def fetch_overdue_issues(now_iso: str, issue_type: str, limit: int = 200) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM issues
        WHERE status='OPEN' AND issue_type=? AND due_ts <= ?
        ORDER BY due_ts ASC
        LIMIT ?
        """,
        (issue_type, now_iso, limit),
    ).fetchall()
    conn.close()
    return rows


def fetch_unnotified_breaches(now_iso: str, limit: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM issues
        WHERE status='OPEN'
          AND due_ts <= ?
          AND breach_notified_ts IS NULL
        ORDER BY due_ts ASC
        LIMIT ?
        """,
        (now_iso, limit),
    ).fetchall()
    conn.close()
    return rows


def fetch_resolved_since(last_ts: Optional[str], now_iso: str) -> List[sqlite3.Row]:
    if not last_ts:
        return []
    conn = db()
    rows = conn.execute(
        """
        SELECT *
        FROM issues
        WHERE status='RESOLVED'
          AND resolved_ts IS NOT NULL
          AND resolved_ts > ?
          AND resolved_ts <= ?
        ORDER BY resolved_ts DESC
        LIMIT 100
        """,
        (last_ts, now_iso),
    ).fetchall()
    conn.close()
    return rows
