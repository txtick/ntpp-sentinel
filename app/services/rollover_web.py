"""Authenticated, technician-scoped Route Rollover web workflow."""

import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from pg import pg
from services.dashboard_backend import (
    GHL_BASE_URL,
    GHL_LOCATION_ID,
    _ghl_headers,
    _skimmer_json_get,
)

log = logging.getLogger("rollover.web")

ROLLOVER_ENABLED = os.getenv("ROLLOVER_ENABLED", "1").lower() in ("1", "true", "yes", "on")
ROLLOVER_WEB_ENABLED = os.getenv("ROLLOVER_WEB_ENABLED", "1").lower() in ("1", "true", "yes", "on")
ROLLOVER_AI_ENABLED = os.getenv("ROLLOVER_AI_ENABLED", "1").lower() in ("1", "true", "yes", "on")
ROLLOVER_AI_MODEL = os.getenv("ROLLOVER_AI_MODEL", os.getenv("AI_GATE_MODEL", "gpt-4o-mini")).strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TIMEZONE_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))
OFFICE_PHONE = "833-689-7665"
MAX_SELECTED_STOPS = 30
MAX_MESSAGE_LENGTH = 600
MIN_MESSAGE_LENGTH = 30
SEND_DELAY_SECONDS = 0.25

DEFAULT_MESSAGE_TEMPLATE = (
    "Hi {{customer_first_name}}, this is {{tech_first_name}} with North Texas Pool Pros. "
    "I'm sorry, but I won't be able to service your pool today. I've moved your service "
    "to tomorrow and will let you know when I'm on the way. If this causes any issues or "
    f"you have any concerns, please reply here or call our office at {OFFICE_PHONE}. "
    "We apologize for the inconvenience."
)


def ensure_schema() -> None:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rollover_web_batches (
                    id BIGSERIAL PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    actor_email TEXT NOT NULL,
                    technician_id BIGINT REFERENCES technicians(id) ON DELETE SET NULL,
                    source_account_id TEXT NOT NULL,
                    service_date DATE NOT NULL,
                    issue_reason TEXT,
                    message_template TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    selected_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    completed_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS rollover_web_items (
                    id BIGSERIAL PRIMARY KEY,
                    batch_id BIGINT NOT NULL REFERENCES rollover_web_batches(id) ON DELETE CASCADE,
                    route_stop_id TEXT NOT NULL,
                    source_customer_id TEXT,
                    source_service_location_id TEXT,
                    customer_name TEXT,
                    address TEXT,
                    rendered_message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_code TEXT,
                    error_message TEXT,
                    ghl_conversation_id TEXT,
                    ghl_contact_id TEXT,
                    sent_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (batch_id, route_stop_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rollover_web_batches_actor_date
                ON rollover_web_batches(actor_email, service_date DESC, created_at DESC)
                """
            )
        conn.commit()


def _require_enabled() -> None:
    if not ROLLOVER_ENABLED or not ROLLOVER_WEB_ENABLED:
        raise HTTPException(status_code=503, detail="Route Rollover web access is disabled")


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _resolve_technician(email: str) -> Dict[str, Any]:
    normalized = _normalize_email(email)
    if not normalized:
        raise HTTPException(status_code=401, detail="Signed-in email is required")
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    source_account_id,
                    email,
                    first_name,
                    last_name,
                    role_type,
                    is_active
                FROM technicians
                WHERE lower(email) = %s
                  AND is_active = TRUE
                ORDER BY updated_at DESC
                LIMIT 2
                """,
                (normalized,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    if not rows:
        raise HTTPException(
            status_code=403,
            detail="Your NTPP email is not linked to an active Skimmer technician account. Contact the office.",
        )
    if len(rows) != 1:
        raise HTTPException(status_code=409, detail="Multiple Skimmer technicians use this email. Contact the office.")
    return rows[0]


def _local_today() -> date:
    return datetime.now(ZoneInfo(TIMEZONE_NAME)).date()


def _parse_complete_time(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.year >= 2011 else None


def _stop_id(stop: Dict[str, Any]) -> str:
    return str(stop.get("id") or stop.get("routeStopId") or "").strip()


def _stop_name(stop: Dict[str, Any]) -> str:
    company = str(stop.get("companyName") or "").strip()
    first = str(stop.get("customerFirstName") or "").strip()
    last = str(stop.get("customerLastName") or "").strip()
    return company or " ".join(part for part in (first, last) if part) or "Customer"


def _public_stop(stop: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": _stop_id(stop),
        "sequence": stop.get("sequence"),
        "customer_id": stop.get("customerId"),
        "service_location_id": stop.get("serviceLocationId"),
        "customer_first_name": str(stop.get("customerFirstName") or "").strip(),
        "customer_name": _stop_name(stop),
        "address": str(stop.get("address") or "").strip(),
        "city": str(stop.get("city") or "").strip(),
        "completed": _parse_complete_time(stop.get("completeTime")) is not None,
        "complete_time": stop.get("completeTime"),
    }


def _fetch_route(technician: Dict[str, Any], service_date: date) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    route = _skimmer_json_get(
        "/Routes/GetTechRoute",
        {"TechId": technician["source_account_id"], "ServiceDate": service_date.isoformat()},
    )
    stops = route.get("stops") if isinstance(route, dict) else None
    return route if isinstance(route, dict) else {}, [s for s in (stops or []) if isinstance(s, dict)]


def _recent_batches(email: str, limit: int = 5) -> List[Dict[str, Any]]:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, service_date, status, selected_count, sent_count, failed_count, created_at, completed_at
                FROM rollover_web_batches
                WHERE actor_email = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (_normalize_email(email), max(1, min(limit, 10))),
            )
            return [dict(row) for row in cur.fetchall()]


def get_route_for_user(email: str) -> Dict[str, Any]:
    _require_enabled()
    technician = _resolve_technician(email)
    today = _local_today()
    route, stops = _fetch_route(technician, today)
    public_stops = [_public_stop(stop) for stop in stops if _stop_id(stop)]
    return {
        "ok": True,
        "service_date": today.isoformat(),
        "technician": {
            "id": technician["id"],
            "source_account_id": technician["source_account_id"],
            "email": technician["email"],
            "first_name": technician.get("first_name") or route.get("techFirstName") or "Tech",
            "last_name": technician.get("last_name") or route.get("techLastName") or "",
        },
        "stops": public_stops,
        "unfinished_count": sum(1 for stop in public_stops if not stop["completed"]),
        "completed_count": sum(1 for stop in public_stops if stop["completed"]),
        "default_message_template": DEFAULT_MESSAGE_TEMPLATE,
        "ai_enabled": bool(ROLLOVER_AI_ENABLED and OPENAI_API_KEY),
        "recent_batches": _recent_batches(email),
    }


def _extract_openai_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = str(content.get("text") or "").strip()
                if text:
                    return text
    return ""


def generate_message(email: str, issue_reason: str) -> Dict[str, Any]:
    _require_enabled()
    technician = _resolve_technician(email)
    issue = " ".join(str(issue_reason or "").split())
    if not issue:
        raise HTTPException(status_code=400, detail="Add a brief issue or reason before using AI")
    if len(issue) > 240:
        raise HTTPException(status_code=400, detail="Issue or reason must be 240 characters or fewer")
    if not ROLLOVER_AI_ENABLED or not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="AI message drafting is not configured")

    tech_first = str(technician.get("first_name") or "your technician").strip()
    instructions = (
        "Write one concise, warm operational SMS template for North Texas Pool Pros. "
        "Use the literal token {{customer_first_name}} exactly once. State that the technician cannot "
        "service the pool today, give the supplied reason tactfully without adding facts, apologize, say "
        "service is moved to tomorrow, and invite the customer to reply with concerns or call 833-689-7665. "
        "Do not offer compensation, mention internal operations, include markdown, or exceed 500 characters."
    )
    payload = {
        "model": ROLLOVER_AI_MODEL,
        "store": False,
        "max_output_tokens": 220,
        "instructions": instructions,
        "input": f"Technician first name: {tech_first}\nIssue/reason: {issue}",
        "safety_identifier": hashlib.sha256(_normalize_email(email).encode("utf-8")).hexdigest()[:32],
    }
    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        log.error("rollover AI failed status=%s detail=%s", exc.code, detail[:500])
        raise HTTPException(status_code=502, detail="AI drafting failed; the standard message is still available")
    except Exception as exc:
        log.error("rollover AI request failed: %s", exc)
        raise HTTPException(status_code=502, detail="AI drafting failed; the standard message is still available")

    text = re.sub(r"\s+", " ", _extract_openai_text(result)).strip().strip('"')
    if "{{customer_first_name}}" not in text or not (MIN_MESSAGE_LENGTH <= len(text) <= MAX_MESSAGE_LENGTH):
        raise HTTPException(status_code=502, detail="AI draft did not pass message validation; use the standard message")
    return {"ok": True, "message_template": text, "model": ROLLOVER_AI_MODEL}


def _render_message(template: str, stop: Dict[str, Any], tech_first_name: str) -> str:
    customer_first = str(stop.get("customerFirstName") or "there").strip() or "there"
    rendered = str(template or "")
    for token in ("{{customer_first_name}}", "{customer_first_name}"):
        rendered = rendered.replace(token, customer_first)
    for token in ("{{tech_first_name}}", "{tech_first_name}"):
        rendered = rendered.replace(token, tech_first_name)
    return re.sub(r"\s+", " ", rendered).strip()


def _normalize_phone(value: Any) -> Optional[str]:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _customer_phone(source_customer_id: Any) -> Optional[str]:
    customer_id = str(source_customer_id or "").strip()
    if not customer_id:
        return None
    try:
        customer = _skimmer_json_get(f"/Customers/{urllib.parse.quote(customer_id)}")
    except HTTPException:
        return None
    if not isinstance(customer, dict):
        return None
    for key in ("phone", "Phone", "mobilePhone", "MobilePhone"):
        phone = _normalize_phone(customer.get(key))
        if phone:
            return phone
    return None


def _ghl_conversation_for_phone(phone: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    url = f"{GHL_BASE_URL}/conversations/search?{urllib.parse.urlencode({'locationId': GHL_LOCATION_ID, 'phone': phone})}"
    req = urllib.request.Request(url, headers=_ghl_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return None, None, f"GHL lookup failed: {exc}"
    matches: List[Tuple[str, str]] = []
    if isinstance(payload, dict):
        for key in ("conversations", "data", "items"):
            rows = payload.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                conversation_id = str(row.get("id") or row.get("conversationId") or "").strip()
                contact_id = str(row.get("contactId") or "").strip()
                if conversation_id and contact_id:
                    matches.append((conversation_id, contact_id))
            if rows:
                break
    unique = list(dict.fromkeys(matches))
    if len(unique) == 1:
        return unique[0][0], unique[0][1], None
    if not unique:
        return None, None, "No matching GHL conversation"
    return None, None, "Multiple GHL conversations matched this phone"


def _ghl_send_sms(conversation_id: str, contact_id: str, message: str) -> None:
    payload = json.dumps(
        {"type": "SMS", "message": message, "conversationId": conversation_id, "contactId": contact_id}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{GHL_BASE_URL}/conversations/messages", data=payload, headers=_ghl_headers(), method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


def _batch_result(batch_id: int, duplicate: bool = False) -> Dict[str, Any]:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM rollover_web_batches WHERE id = %s", (batch_id,))
            batch = dict(cur.fetchone())
            cur.execute(
                """
                SELECT route_stop_id, customer_name, address, status, error_code, error_message, sent_at
                FROM rollover_web_items WHERE batch_id = %s ORDER BY id
                """,
                (batch_id,),
            )
            items = [dict(row) for row in cur.fetchall()]
    return {"ok": batch["status"] == "complete", "duplicate": duplicate, "batch": batch, "items": items}


def send_rollover(email: str, body: Dict[str, Any]) -> Dict[str, Any]:
    _require_enabled()
    technician = _resolve_technician(email)
    selected_ids = list(dict.fromkeys(str(value or "").strip() for value in (body.get("stop_ids") or [])))
    selected_ids = [value for value in selected_ids if value]
    if not selected_ids:
        raise HTTPException(status_code=400, detail="Select at least one unfinished customer")
    if len(selected_ids) > MAX_SELECTED_STOPS:
        raise HTTPException(status_code=400, detail=f"Select no more than {MAX_SELECTED_STOPS} customers at once")
    template = re.sub(r"\s+", " ", str(body.get("message_template") or "")).strip()
    if not (MIN_MESSAGE_LENGTH <= len(template) <= MAX_MESSAGE_LENGTH):
        raise HTTPException(
            status_code=400,
            detail=f"Message must be between {MIN_MESSAGE_LENGTH} and {MAX_MESSAGE_LENGTH} characters",
        )
    issue = " ".join(str(body.get("issue_reason") or "").split())[:240] or None
    idempotency_key = str(body.get("idempotency_key") or "").strip()
    try:
        uuid.UUID(idempotency_key)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="A valid submission id is required")

    today = _local_today()
    _route, stops = _fetch_route(technician, today)
    route_by_id = {_stop_id(stop): stop for stop in stops if _stop_id(stop)}
    missing = [stop_id for stop_id in selected_ids if stop_id not in route_by_id]
    if missing:
        raise HTTPException(status_code=409, detail="The route changed. Refresh and review the customer list again.")
    completed = [stop_id for stop_id in selected_ids if _parse_complete_time(route_by_id[stop_id].get("completeTime"))]
    if completed:
        raise HTTPException(status_code=409, detail="A selected customer is already complete. Refresh the route before sending.")

    tech_first = str(technician.get("first_name") or "your technician").strip()
    rendered = {stop_id: _render_message(template, route_by_id[stop_id], tech_first) for stop_id in selected_ids}
    if any(not (MIN_MESSAGE_LENGTH <= len(message) <= MAX_MESSAGE_LENGTH) for message in rendered.values()):
        raise HTTPException(status_code=400, detail="A personalized message is outside the allowed SMS length")

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rollover_web_batches (
                    idempotency_key, actor_email, technician_id, source_account_id, service_date,
                    issue_reason, message_template, selected_count
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    idempotency_key,
                    _normalize_email(email),
                    technician["id"],
                    technician["source_account_id"],
                    today,
                    issue,
                    template,
                    len(selected_ids),
                ),
            )
            inserted = cur.fetchone()
            if not inserted:
                cur.execute("SELECT id, actor_email FROM rollover_web_batches WHERE idempotency_key = %s", (idempotency_key,))
                existing = cur.fetchone()
                if not existing or existing["actor_email"] != _normalize_email(email):
                    raise HTTPException(status_code=409, detail="Submission id is already in use")
                conn.commit()
                return _batch_result(int(existing["id"]), duplicate=True)
            batch_id = int(inserted["id"])
            for stop_id in selected_ids:
                stop = route_by_id[stop_id]
                cur.execute(
                    """
                    INSERT INTO rollover_web_items (
                        batch_id, route_stop_id, source_customer_id, source_service_location_id,
                        customer_name, address, rendered_message
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        batch_id,
                        stop_id,
                        stop.get("customerId"),
                        stop.get("serviceLocationId"),
                        _stop_name(stop),
                        stop.get("address"),
                        rendered[stop_id],
                    ),
                )
        conn.commit()

    sent_count = 0
    failed_count = 0
    for stop_id in selected_ids:
        stop = route_by_id[stop_id]
        error_code = None
        error_message = None
        conversation_id = None
        contact_id = None
        try:
            phone = _customer_phone(stop.get("customerId"))
            if not phone:
                error_code, error_message = "missing_phone", "No valid phone number in Skimmer"
            else:
                conversation_id, contact_id, lookup_error = _ghl_conversation_for_phone(phone)
                if lookup_error:
                    error_code, error_message = "conversation_match", lookup_error
                else:
                    _ghl_send_sms(str(conversation_id), str(contact_id), rendered[stop_id])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error_code, error_message = "ghl_send", f"GHL send failed ({exc.code}): {detail[:180]}"
        except Exception as exc:
            error_code, error_message = "send_error", str(exc)[:240]

        status = "failed" if error_code else "sent"
        sent_count += 1 if status == "sent" else 0
        failed_count += 1 if status == "failed" else 0
        with pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE rollover_web_items
                    SET status = %s, error_code = %s, error_message = %s,
                        ghl_conversation_id = %s, ghl_contact_id = %s,
                        sent_at = CASE WHEN %s = 'sent' THEN NOW() ELSE sent_at END
                    WHERE batch_id = %s AND route_stop_id = %s AND status = 'pending'
                    """,
                    (status, error_code, error_message, conversation_id, contact_id, status, batch_id, stop_id),
                )
            conn.commit()
        time.sleep(SEND_DELAY_SECONDS)

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE rollover_web_batches
                SET status = 'complete', sent_count = %s, failed_count = %s, completed_at = NOW()
                WHERE id = %s
                """,
                (sent_count, failed_count, batch_id),
            )
        conn.commit()
    log.info(
        "rollover.web.complete actor=%s tech=%s batch=%s selected=%s sent=%s failed=%s",
        _normalize_email(email), technician["source_account_id"], batch_id, len(selected_ids), sent_count, failed_count,
    )
    return _batch_result(batch_id)
