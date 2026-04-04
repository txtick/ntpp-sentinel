"""
Route Rollover Service
======================
Skimmer API access, session CRUD, message builders, and number parsing
for the rollover state machine. No GHL or SQLite connections are opened
here — callers pass those in.
"""
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx  # type: ignore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (read directly from env — same pattern as ai_gate.py)
# ---------------------------------------------------------------------------
SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "")
ROLLOVER_ENABLED = os.getenv("ROLLOVER_ENABLED", "1").lower() in ("1", "true", "yes", "on")

OFFICE_PHONE = "833-689-7665"
MAX_STOPS_SHOWN = 20
CUSTOMER_SEND_DELAY_SECONDS = 0.5

TRIGGER_PHRASES = [
    "rollover",
    "roll over",
    "not finishing",
    "not going to finish",
    "cant finish",
    "can't finish",
]


# ---------------------------------------------------------------------------
# Trigger detection
# ---------------------------------------------------------------------------

def is_rollover_trigger(text: str) -> bool:
    lower = (text or "").lower()
    return any(phrase in lower for phrase in TRIGGER_PHRASES)


# ---------------------------------------------------------------------------
# Skimmer API
# ---------------------------------------------------------------------------

async def _skimmer_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = SKIMMER_API_BASE_URL.rstrip("/") + path
    headers = {"skimmer-api-key": SKIMMER_API_KEY}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers=headers, params=params or {})
    r.raise_for_status()
    return r.json()


async def fetch_tech_route(skimmer_tech_id: str, service_date: str) -> Dict[str, Any]:
    """GET /Routes/GetTechRoute — raises on HTTP error."""
    return await _skimmer_get(
        "/Routes/GetTechRoute",
        params={"TechId": skimmer_tech_id, "ServiceDate": service_date},
    )


async def fetch_skimmer_customer(skimmer_customer_id: str) -> Optional[Dict[str, Any]]:
    """GET /Customers/{id} — returns None on any error."""
    try:
        return await _skimmer_get(f"/Customers/{skimmer_customer_id}")
    except Exception as exc:
        log.error("rollover: skimmer customer lookup failed customer=%s: %s", skimmer_customer_id, exc)
        return None


def extract_customer_phone(customer: Dict[str, Any]) -> Optional[str]:
    """Pull phone from a Skimmer customer record; prefers phone over mobilePhone."""
    for key in ("phone", "Phone", "mobilePhone", "MobilePhone"):
        val = (customer.get(key) or "").strip()
        if val:
            return val
    return None


# ---------------------------------------------------------------------------
# Session CRUD  (callers open/close the SQLite connection)
# ---------------------------------------------------------------------------

def get_active_session(conn, contact_id: str):
    """Return the most recent AWAITING_* session for this contact, or None."""
    return conn.execute(
        """
        SELECT * FROM rollover_sessions
        WHERE contact_id = ? AND state IN ('AWAITING_SELECTION', 'AWAITING_CONFIRMATION')
        ORDER BY id DESC LIMIT 1
        """,
        (contact_id,),
    ).fetchone()


def abort_incomplete_sessions(conn, contact_id: str) -> None:
    """Mark any AWAITING_* sessions for this contact as ABORTED."""
    conn.execute(
        """
        UPDATE rollover_sessions
        SET state = 'ABORTED', updated_ts = ?
        WHERE contact_id = ? AND state IN ('AWAITING_SELECTION', 'AWAITING_CONFIRMATION')
        """,
        (time.time(), contact_id),
    )
    conn.commit()


def create_session(
    conn,
    contact_id: str,
    conversation_id: Optional[str],
    route_stops: List[Dict[str, Any]],
) -> int:
    """Insert a new AWAITING_SELECTION session; returns the new row id."""
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO rollover_sessions
          (contact_id, conversation_id, state, route_stops_json, created_ts, updated_ts)
        VALUES (?, ?, 'AWAITING_SELECTION', ?, ?, ?)
        """,
        (contact_id, conversation_id, json.dumps(route_stops), now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_session_state(
    conn,
    session_id: int,
    state: str,
    selected_indices: Optional[str] = None,
    mark_completed: bool = False,
) -> None:
    now = time.time()
    completed_ts = now if mark_completed else None
    conn.execute(
        """
        UPDATE rollover_sessions
        SET state = ?,
            updated_ts = ?,
            selected_indices = COALESCE(?, selected_indices),
            completed_ts = COALESCE(?, completed_ts)
        WHERE id = ?
        """,
        (state, now, selected_indices, completed_ts, session_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------

def _stop_line(stop: Dict[str, Any]) -> str:
    first = (stop.get("customerFirstName") or "").strip()
    address = (stop.get("address") or "").strip()
    if first and address:
        return f"{first} - {address}"
    return first or address or "(unknown)"


def build_route_list_message(stops: List[Dict[str, Any]]) -> str:
    shown = stops[:MAX_STOPS_SHOWN]
    lines = [
        "Here are your stops for today. Reply with the numbers of customers "
        "you CANNOT service (e.g. 2 4 5 or 2,4,5):\n"
    ]
    for i, stop in enumerate(shown, 1):
        lines.append(f"{i}. {_stop_line(stop)}")
    if len(stops) > MAX_STOPS_SHOWN:
        lines.append(f"(Showing first {MAX_STOPS_SHOWN} of {len(stops)} stops)")
    return "\n".join(lines)


def build_confirmation_message(selected_stops: List[Dict[str, Any]], tech_first_name: str) -> str:
    lines = ["I'll send the following customers an apology message from you:\n"]
    for i, stop in enumerate(selected_stops, 1):
        lines.append(f"{i}. {_stop_line(stop)}")

    example_name = (
        (selected_stops[0].get("customerFirstName") or "there").strip()
        if selected_stops
        else "there"
    )
    tname = tech_first_name or "your tech"
    lines.append(
        f'\nMessage they\'ll receive:\n'
        f'"Hello {example_name}, this is {tname} with North Texas Pool Pros. '
        f"I apologize but I'm not going to be able to make it to your pool today. "
        f"I will service the pool in the morning and I will let you know when I'm on my way. "
        f"I apologize for any inconvenience and if you have any concerns or comments, "
        f'please call our office at {OFFICE_PHONE}, or reply to this text"'
    )
    lines.append("\nReply YES to send, or NO to cancel.")
    return "\n".join(lines)


def build_customer_message(customer_first_name: str, tech_first_name: str) -> str:
    cname = (customer_first_name or "there").strip() or "there"
    tname = (tech_first_name or "your tech").strip() or "your tech"
    return (
        f"Hello {cname}, this is {tname} with North Texas Pool Pros. "
        f"I apologize but I'm not going to be able to make it to your pool today. "
        f"I will service the pool in the morning and I will let you know when I'm on my way. "
        f"I apologize for any inconvenience and if you have any concerns or comments, "
        f"please call our office at {OFFICE_PHONE}, or reply to this text"
    )


def build_manager_message(
    tech_first_name: str,
    tech_contact_id: str,
    notified: List[Dict[str, Any]],
    failed: List[Dict[str, Any]],
    sent_time: str,
) -> str:
    tname = tech_first_name or tech_contact_id
    lines = [f"\U0001f504 Route Rollover \u2014 {tname} ({tech_contact_id})\n"]
    lines.append(f"{len(notified)} customer(s) notified:")
    for stop in notified:
        lines.append(f"- {_stop_line(stop)}")
    if failed:
        lines.append("\n\u26a0\ufe0f Failed to send to:")
        for stop in failed:
            lines.append(f"- {_stop_line(stop)}")
    lines.append(f"\nSent at {sent_time} CT")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def parse_stop_numbers(text: str, max_n: int) -> Tuple[Optional[List[int]], Optional[str]]:
    """
    Parse "2 4 5", "2,4,5", "2, 4, 5" into a deduplicated list of 1-based ints.
    Returns (numbers, None) on success or (None, error_message) on failure.
    """
    cleaned = (text or "").replace(",", " ")
    tokens = [t.strip() for t in cleaned.split() if t.strip()]
    if not tokens:
        return None, "I didn't understand that. Please reply with just the stop numbers, like: 2 4 5"

    for t in tokens:
        if not t.isdigit():
            return None, "I didn't understand that. Please reply with just the stop numbers, like: 2 4 5"

    numbers = [int(t) for t in tokens]
    out_of_range = [n for n in numbers if n < 1 or n > max_n]
    if out_of_range:
        return None, f"Some numbers were out of range. Please choose from 1 to {max_n}."

    seen: set = set()
    deduped: List[int] = []
    for n in numbers:
        if n not in seen:
            deduped.append(n)
            seen.add(n)
    return deduped, None


def parse_yes_no(text: str) -> Optional[str]:
    """Returns 'YES', 'NO', or None if the reply is unrecognized."""
    lower = (text or "").strip().lower()
    if lower in ("yes", "y", "yep", "yeah", "confirm", "send", "ok", "okay"):
        return "YES"
    if lower in ("no", "n", "nope", "cancel", "stop", "abort", "nah"):
        return "NO"
    return None
