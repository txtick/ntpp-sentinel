"""Sales Assist service — open quote pipeline with AI-generated sales notes."""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

import httpx

from pg import pg

logger = logging.getLogger("sales_assist")

SALES_ASSIST_AI_MODEL = os.getenv("SALES_ASSIST_AI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
GHL_APP_BASE_URL = os.getenv("GHL_APP_BASE_URL", "https://app.gohighlevel.com").rstrip("/")
GHL_CONTACT_URL_TEMPLATE = os.getenv("GHL_CONTACT_URL_TEMPLATE", "").strip()

_OPEN_STATUSES = ("Sent",)
_ALL_STATUSES = ("Sent", "Draft", "Approved", "Rejected", "Archived")


def _ghl_contact_url(contact_id: Optional[str]) -> Optional[str]:
    contact_id = str(contact_id or "").strip()
    location_id = os.getenv("GHL_LOCATION_ID", "").strip()
    if not contact_id or not location_id:
        return None
    safe_contact_id = quote(contact_id, safe="")
    safe_location_id = quote(location_id, safe="")
    if GHL_CONTACT_URL_TEMPLATE:
        return GHL_CONTACT_URL_TEMPLATE.format(
            contact_id=safe_contact_id,
            location_id=safe_location_id,
        )
    return f"{GHL_APP_BASE_URL}/v2/location/{safe_location_id}/contacts/detail/{safe_contact_id}"


# ── Priority scoring ────────────────────────────────────────────────────────

def _compute_priority(quote: dict, activities: list) -> tuple[int, list[str]]:
    score = 0
    reasons = []
    now = datetime.now(timezone.utc)

    amount = float(quote.get("total_amount") or 0)
    if amount >= 2000:
        score += 25
        reasons.append(f"High value quote (${amount:,.0f})")
    elif amount >= 500:
        score += 10
        reasons.append(f"Mid-value quote (${amount:,.0f})")

    status = (quote.get("status") or "").lower()
    if status == "sent":
        score += 15
        reasons.append("Quote sent to customer — awaiting response")
    elif status == "draft":
        score += 5
        reasons.append("Draft — not yet sent to customer")
    quote_date = quote.get("quote_date")
    if quote_date:
        if isinstance(quote_date, str):
            try:
                quote_date = datetime.fromisoformat(quote_date.replace("Z", "+00:00"))
            except Exception:
                quote_date = None
        if quote_date:
            if quote_date.tzinfo is None:
                quote_date = quote_date.replace(tzinfo=timezone.utc)
            age_days = (now - quote_date).days
            if 7 <= age_days <= 14:
                score += 10
                reasons.append(f"Quote is {age_days} days old — prime follow-up window")
            elif 15 <= age_days <= 30:
                score += 15
                reasons.append(f"Quote is {age_days} days old — needs follow-up soon")
            elif age_days > 30:
                score += 5
                reasons.append(f"Quote is {age_days} days old — long-outstanding")

    if quote.get("is_active_customer"):
        score += 20
        reasons.append("Active recurring service customer")

    if activities:
        latest_ts_str = max((a.get("created_at") or ""), key=str)
        try:
            latest_ts = datetime.fromisoformat(str(latest_ts_str).replace("Z", "+00:00"))
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=timezone.utc)
            days_since = (now - latest_ts).days
            if days_since >= 14:
                score += 12
                reasons.append(f"No follow-up in {days_since} days")
            elif days_since >= 7:
                score += 8
                reasons.append(f"No follow-up in {days_since} days")
        except Exception:
            score += 8
            reasons.append("No recent follow-up activity")
    else:
        score += 10
        reasons.append("No follow-up activity recorded")

    return min(score, 100), reasons


def _parse_ts(val) -> Optional[datetime]:
    if not val:
        return None
    if isinstance(val, datetime):
        return val
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fmt_date(val) -> Optional[str]:
    dt = _parse_ts(val)
    return dt.strftime("%Y-%m-%d") if dt else None


# ── Quote list ──────────────────────────────────────────────────────────────

def list_quotes(
    statuses: Optional[list] = None,
    city: Optional[str] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    age_min_days: Optional[int] = None,
    age_max_days: Optional[int] = None,
    active_customer_only: bool = False,
    overdue_follow_up_only: bool = False,
    search: Optional[str] = None,
    sort: str = "priority",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    if statuses is None:
        statuses = list(_OPEN_STATUSES)

    conditions = [
        "q.source_system = 'skimmer'",
        "q.is_deleted = FALSE",
        "q.is_archived = FALSE",
    ]
    params: list = []

    if statuses:
        placeholders = ",".join(["%s"] * len(statuses))
        conditions.append(f"q.status IN ({placeholders})")
        params.extend(statuses)

    if city:
        conditions.append("LOWER(q.customer_city) LIKE LOWER(%s)")
        params.append(f"%{city}%")

    if min_amount is not None:
        conditions.append("q.total_amount >= %s")
        params.append(min_amount)

    if max_amount is not None:
        conditions.append("q.total_amount <= %s")
        params.append(max_amount)

    if search:
        conditions.append("""(
            COALESCE(
                NULLIF(TRIM(q.customer_display_name), ''),
                NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''),
                c.company_name
            ) ILIKE %s
            OR q.quote_number::text ILIKE %s
        )""")
        params.extend([f"%{search}%", f"%{search}%"])

    now = datetime.now(timezone.utc)
    if age_min_days is not None:
        cutoff = now.isoformat()
        conditions.append(f"q.quote_date <= NOW() - INTERVAL '{age_min_days} days'")
    if age_max_days is not None:
        conditions.append(f"q.quote_date >= NOW() - INTERVAL '{age_max_days} days'")

    where = " AND ".join(conditions)

    # Skimmer encodes "no expiration" as year 10000 which psycopg cannot
    # deserialize as a Python datetime. Clamp to NULL in the query.
    _safe_ts = "CASE WHEN {col} IS NOT NULL AND EXTRACT(YEAR FROM {col}) < 10000 THEN {col} ELSE NULL END"

    sql = f"""
        SELECT
            q.source_quote_id,
            q.quote_number,
            q.status,
            {_safe_ts.format(col='q.quote_date')} AS quote_date,
            {_safe_ts.format(col='q.expiration_date')} AS expiration_date,
            {_safe_ts.format(col='q.sent_date')} AS sent_date,
            q.total_amount,
            q.subtotal_amount,
            COALESCE(
                NULLIF(TRIM(q.customer_display_name), ''),
                NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''),
                c.company_name
            ) AS customer_display_name,
            q.customer_city,
            q.customer_state,
            q.customer_zip,
            q.internal_notes,
            q.reject_reason,
            c.phone AS customer_phone,
            c.mobile_phone AS customer_mobile,
            c.email AS customer_email,
            c.customer_status,
            c.ghl_contact_id,
            EXISTS(
                SELECT 1 FROM sk_route_assignment ra
                JOIN sk_service_location sl
                    ON sl.source_location_id = ra.source_service_location_id
                    AND sl.source_system = ra.source_system
                WHERE sl.source_customer_id = q.source_customer_id
                  AND ra.source_system = 'skimmer'
                  AND ra.is_deleted = FALSE
            ) AS is_active_customer,
            (SELECT MAX(created_at) FROM quote_sales_activities
             WHERE source_quote_id = q.source_quote_id) AS last_activity_at,
            (SELECT activity_type FROM quote_sales_activities
             WHERE source_quote_id = q.source_quote_id
             ORDER BY created_at DESC LIMIT 1) AS last_activity_type,
            (SELECT follow_up_at FROM quote_sales_activities
             WHERE source_quote_id = q.source_quote_id
               AND follow_up_at IS NOT NULL
             ORDER BY created_at DESC LIMIT 1) AS next_follow_up_at,
            (SELECT status FROM quote_ai_sales_notes
             WHERE source_quote_id = q.source_quote_id LIMIT 1) AS ai_notes_status
        FROM sk_quote q
        LEFT JOIN sk_customer c
            ON c.source_customer_id = q.source_customer_id
            AND c.source_system = 'skimmer'
        WHERE {where}
    """

    count_sql = f"""
        SELECT COUNT(*) AS total FROM sk_quote q
        LEFT JOIN sk_customer c
            ON c.source_customer_id = q.source_customer_id
            AND c.source_system = 'skimmer'
        WHERE {where}
    """

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(count_sql, params)
            total = (cur.fetchone() or {}).get("total", 0)
            cur.execute(sql, params)
            rows = cur.fetchall()

    # Enrich with priority scores
    results = []
    for row in rows:
        q = dict(row)
        activities_for_score = []
        if q.get("last_activity_at"):
            activities_for_score = [{"created_at": str(q["last_activity_at"])}]
        score, reasons = _compute_priority(q, activities_for_score)
        q["priority_score"] = score
        q["priority_reasons"] = reasons
        q["quote_date"] = _fmt_date(q.get("quote_date"))
        q["expiration_date"] = _fmt_date(q.get("expiration_date"))
        q["sent_date"] = _fmt_date(q.get("sent_date"))
        q["last_activity_at"] = _fmt_date(q.get("last_activity_at"))
        q["next_follow_up_at"] = _fmt_date(q.get("next_follow_up_at"))
        now_dt = datetime.now(timezone.utc)
        if q.get("quote_date"):
            try:
                qd = datetime.fromisoformat(str(row.get("quote_date") or "").replace("Z", "+00:00"))
                if qd.tzinfo is None:
                    qd = qd.replace(tzinfo=timezone.utc)
                q["age_days"] = (now_dt - qd).days
            except Exception:
                q["age_days"] = None
        else:
            q["age_days"] = None
        results.append(q)

    if overdue_follow_up_only:
        results = [r for r in results if not r.get("next_follow_up_at")]

    if active_customer_only:
        results = [r for r in results if r.get("is_active_customer")]

    # Sort
    if sort == "priority":
        results.sort(key=lambda r: r.get("priority_score") or 0, reverse=True)
    elif sort == "newest":
        results.sort(key=lambda r: r.get("quote_date") or "", reverse=True)
    elif sort == "oldest":
        results.sort(key=lambda r: r.get("quote_date") or "")
    elif sort == "highest_amount":
        results.sort(key=lambda r: float(r.get("total_amount") or 0), reverse=True)
    elif sort == "no_recent_follow_up":
        results.sort(key=lambda r: r.get("last_activity_at") or "")

    total_after_filter = len(results)
    results = results[offset:offset + limit]

    return {"quotes": results, "total": total_after_filter, "db_total": total}


# ── Quote detail ────────────────────────────────────────────────────────────

def get_quote_detail(source_quote_id: str) -> dict:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    q.source_quote_id, q.source_customer_id, q.quote_number, q.status,
                    q.total_amount, q.subtotal_amount, q.tax_amount,
                    q.discount_flat, q.discount_percent, q.discount_total,
                    q.customer_display_name, q.customer_address,
                    q.customer_city, q.customer_state, q.customer_zip,
                    q.message, q.internal_notes, q.reject_reason,
                    q.is_archived, q.is_deleted, q.job_id,
                    CASE WHEN q.quote_date IS NOT NULL AND EXTRACT(YEAR FROM q.quote_date) < 10000
                         THEN q.quote_date ELSE NULL END AS quote_date,
                    CASE WHEN q.expiration_date IS NOT NULL AND EXTRACT(YEAR FROM q.expiration_date) < 10000
                         THEN q.expiration_date ELSE NULL END AS expiration_date,
                    CASE WHEN q.sent_date IS NOT NULL AND EXTRACT(YEAR FROM q.sent_date) < 10000
                         THEN q.sent_date ELSE NULL END AS sent_date,
                    COALESCE(
                        NULLIF(TRIM(q.customer_display_name), ''),
                        NULLIF(TRIM(COALESCE(c.first_name,'') || ' ' || COALESCE(c.last_name,'')), ''),
                        c.company_name
                    ) AS customer_display_name,
                    c.phone AS customer_phone,
                    c.mobile_phone AS customer_mobile,
                    c.email AS customer_email,
                    c.customer_status,
                    c.company_name AS customer_company_name,
                    c.first_name AS customer_first_name,
                    c.last_name AS customer_last_name,
                    c.ghl_contact_id,
                    EXISTS(
                        SELECT 1 FROM sk_route_assignment ra
                        JOIN sk_service_location sl
                            ON sl.source_location_id = ra.source_service_location_id
                            AND sl.source_system = ra.source_system
                        WHERE sl.source_customer_id = q.source_customer_id
                          AND ra.source_system = 'skimmer'
                          AND ra.is_deleted = FALSE
                    ) AS is_active_customer
                FROM sk_quote q
                LEFT JOIN sk_customer c
                    ON c.source_customer_id = q.source_customer_id
                    AND c.source_system = 'skimmer'
                WHERE q.source_quote_id = %s AND q.source_system = 'skimmer'
                """,
                (source_quote_id,),
            )
            quote = cur.fetchone()
            if not quote:
                return {}

            # Line items (via quote_location join)
            cur.execute(
                """
                SELECT
                    qi.source_quote_item_id,
                    qi.item_name,
                    qi.description,
                    qi.quantity,
                    qi.unit_price,
                    qi.total_price,
                    qi.sequence,
                    qi.is_taxable,
                    qi.source_product_id,
                    ql.source_service_location_id,
                    ql.address AS location_address,
                    ql.city AS location_city,
                    ql.state AS location_state,
                    ql.zip AS location_zip
                FROM sk_quote_item qi
                JOIN sk_quote_location ql
                    ON ql.source_quote_location_id = qi.source_quote_location_id
                    AND ql.source_system = qi.source_system
                WHERE qi.source_quote_id = %s AND qi.source_system = 'skimmer'
                ORDER BY ql.id, qi.sequence
                """,
                (source_quote_id,),
            )
            items = cur.fetchall()

            # Pool info from first service location on the quote
            cur.execute(
                """
                SELECT ql.source_service_location_id
                FROM sk_quote_location ql
                WHERE ql.source_quote_id = %s AND ql.source_system = 'skimmer'
                LIMIT 1
                """,
                (source_quote_id,),
            )
            ql_row = cur.fetchone()
            pool_info = None
            recent_work_orders = []
            if ql_row and ql_row.get("source_service_location_id"):
                sl_id = ql_row["source_service_location_id"]
                cur.execute(
                    """
                    SELECT p.name, p.gallons, p.baseline_filter_pressure, p.notes,
                           p.equipment_items, s.address, s.city, s.state, s.notes AS sl_notes
                    FROM sk_pool p
                    JOIN sk_service_location s
                        ON s.source_location_id = p.source_service_location_id
                        AND s.source_system = p.source_system
                    WHERE p.source_service_location_id = %s AND p.source_system = 'skimmer'
                    LIMIT 1
                    """,
                    (sl_id,),
                )
                pool_info = cur.fetchone()

                cur.execute(
                    """
                    SELECT wo.service_date, wo.work_needed, wo.work_performed, wo.notes,
                           wot.description AS work_order_type
                    FROM sk_work_order wo
                    LEFT JOIN sk_work_order_type wot
                        ON wot.source_work_order_type_id = wo.source_work_order_type_id
                        AND wot.source_system = wo.source_system
                    WHERE wo.source_service_location_id = %s
                      AND wo.source_system = 'skimmer'
                      AND wo.is_deleted = FALSE
                    ORDER BY wo.service_date DESC
                    LIMIT 5
                    """,
                    (sl_id,),
                )
                recent_work_orders = cur.fetchall()

    result = dict(quote)
    result["raw_json"] = None  # don't expose raw
    result["ghl_contact_url"] = _ghl_contact_url(result.get("ghl_contact_id"))
    result["line_items"] = [dict(i) for i in items]
    result["pool_info"] = dict(pool_info) if pool_info else None
    if result.get("pool_info") and isinstance(result["pool_info"].get("equipment_items"), str):
        try:
            result["pool_info"]["equipment_items"] = json.loads(result["pool_info"]["equipment_items"])
        except Exception:
            pass
    result["recent_work_orders"] = [dict(wo) for wo in recent_work_orders]
    result["quote_date"] = _fmt_date(result.get("quote_date"))
    result["expiration_date"] = _fmt_date(result.get("expiration_date"))
    result["sent_date"] = _fmt_date(result.get("sent_date"))
    return result


# ── Activities ──────────────────────────────────────────────────────────────

VALID_ACTIVITY_TYPES = {
    "called", "voicemail", "texted", "emailed",
    "interested", "not_interested", "waiting_on_customer",
    "follow_up_scheduled", "note", "skipped",
}


def create_activity(source_quote_id: str, data: dict) -> dict:
    activity_type = (data.get("activity_type") or "").strip().lower()
    if activity_type not in VALID_ACTIVITY_TYPES:
        raise ValueError(f"Invalid activity_type: {activity_type!r}")

    # Lookup customer_id from quote
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_customer_id FROM sk_quote WHERE source_quote_id = %s AND source_system = 'skimmer'",
                (source_quote_id,),
            )
            row = cur.fetchone()
            customer_id = row["source_customer_id"] if row else None

            follow_up_at = data.get("follow_up_at") or None
            if follow_up_at:
                try:
                    follow_up_at = datetime.fromisoformat(follow_up_at.replace("Z", "+00:00"))
                except Exception:
                    follow_up_at = None

            cur.execute(
                """
                INSERT INTO quote_sales_activities
                    (source_quote_id, source_system, source_customer_id, activity_type,
                     activity_note, created_by, follow_up_at)
                VALUES (%s, 'skimmer', %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    source_quote_id,
                    customer_id,
                    activity_type,
                    data.get("activity_note") or None,
                    data.get("created_by") or None,
                    follow_up_at,
                ),
            )
            new_row = cur.fetchone()
        conn.commit()

    return {"id": new_row["id"], "created_at": str(new_row["created_at"])}


def list_activities(source_quote_id: str) -> list:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, activity_type, activity_note, created_by,
                       follow_up_at, created_at
                FROM quote_sales_activities
                WHERE source_quote_id = %s AND source_system = 'skimmer'
                ORDER BY created_at DESC
                """,
                (source_quote_id,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ── AI notes ────────────────────────────────────────────────────────────────

_AI_SYSTEM_PROMPT = """You are a sales coach for North Texas Pool Pros, a residential pool maintenance and repair company in the Dallas–Fort Worth area.

Your job: given a quote's actual line items and customer context, produce specific, quote-grounded sales coaching notes for a salesperson making a follow-up call.

═══════════════════════════════════════════════════
RULE #1 — BE LINE-ITEM SPECIFIC. THIS IS NON-NEGOTIABLE.
═══════════════════════════════════════════════════
Every section must be grounded in the ACTUAL line items from this quote.
- If a line item is "Drain & Clean", explain what a drain and clean does and why a customer needs it — not "regular maintenance helps your pool."
- If a line item is "Sand Media", explain what sand media is, why it is replaced, and what problem it solves.
- If a line item is "Variable Speed Pump", explain energy efficiency and quieter operation — but only if it is actually a variable speed pump.
- If a line item is "Salt Cell", explain chlorine generation and reduced manual chlorine dependency.
- If a line item is "Heater Repair", focus on comfort, usability, extending swim season.
- If a line item is vague (e.g., "Misc. Labor", "Misc. Plumbing"), acknowledge the vagueness and mark what you are inferring vs. what is confirmed.
Do NOT produce generic talking points that would apply to any pool quote. Talking points must reference the actual items in this quote.

═══════════════════════════════════════════════════
RULE #2 — GUARDRAILS. NEVER do the following:
═══════════════════════════════════════════════════
- Do NOT claim exact energy savings percentages unless the equipment model and operating assumptions are in the quote
- Do NOT claim warranty terms unless the quote explicitly states them
- Do NOT claim rebates or incentives unless confirmed
- Do NOT say equipment is unsafe unless technician notes or source data explicitly support that
- Do NOT claim the work is urgently needed unless the quote notes or service history confirm it
- Do NOT invent model-specific features unless the brand and model are in the quote data
- Do NOT use phrases like "regular maintenance helps prolong equipment life" for repair/replacement quotes

═══════════════════════════════════════════════════
RULE #3 — QUOTE CATEGORY INFERENCE
═══════════════════════════════════════════════════
Inspect the line items and infer the quote category or categories. Use these categories (a quote may have multiple):
drain_and_clean, filter_cleaning, filter_media_replacement, pump_repair, pump_replacement,
variable_speed_pump_upgrade, cleaner_repair, salt_system_repair, salt_cell_replacement,
automation_repair, heater_repair, plumbing_repair, leak_related_repair, light_repair,
algae_treatment, green_to_clean, general_labor, equipment_replacement, misc_repair,
maintenance, unknown_or_unclear

TONE: Professional, consultative, helpful. Never high-pressure. Sound like a real person, not a script.

Return ONLY a valid JSON object. No markdown. No explanation text outside the JSON.

JSON SCHEMA (every field required):
{
  "quote_summary": "2-4 sentences: what is this quote actually for? Reference the specific line items.",
  "quote_categories": [{"category": "string from list above", "reason": "which line item(s) triggered this"}],
  "what_we_are_solving": "The specific customer problem these line items address. Be cautious if data is vague.",
  "confirmed_quote_facts": [
    "Only facts directly from the quote — line items, quantities, prices, totals, quote age, customer status"
  ],
  "sales_positioning": "How the salesperson should frame this quote. Be specific to the work being done.",
  "customer_benefits": [
    "Specific benefit tied to this quote's actual line items. No generic pool advice."
  ],
  "efficiency_reliability_points": [
    "Efficiency or reliability point specific to this quote type. Omit if not applicable."
  ],
  "risks_of_waiting": [
    "Honest, specific risk of deferring this work. No scare tactics. Omit if genuinely low urgency."
  ],
  "likely_objections": [
    {
      "objection": "Realistic objection a customer might raise about THIS quote",
      "suggested_response": "Response that references the actual line items"
    }
  ],
  "call_opening": "A natural, specific opening for this customer and quote. Use the customer's first name and reference the specific work.",
  "closing_script": "A friendly, direct close that references the work.",
  "sms_follow_up": "A specific SMS — do NOT use generic pool maintenance wording unless this is a maintenance quote.",
  "email_follow_up": "A short, specific email body referencing the actual work.",
  "questions_to_ask": [
    "A specific question to help close or clarify this particular quote"
  ],
  "claims_to_avoid": [
    "Something NOT to say — always populate this section with at least 3 items specific to this quote"
  ],
  "missing_information": [
    "Specific data that is absent from this quote and would help the salesperson — e.g., technician diagnosis, equipment brand/model, reason for replacement"
  ],
  "confidence": "high|medium|low",
  "confidence_explanation": "Why this confidence level? e.g., 'Medium — line items are specific but descriptions are blank, so some assumptions were made.'"
}"""


def _item_line_total(item: dict) -> float:
    qty = float(item.get("quantity") or 1)
    unit = float(item.get("unit_price") or 0)
    return qty * unit


def _build_quote_prompt(quote: dict, items: list, pool_info: Optional[dict], work_orders: list) -> str:
    customer_name = quote.get("customer_display_name") or "Unknown Customer"
    first_name = (quote.get("customer_first_name") or customer_name.split()[0] if customer_name else "Customer")
    city = quote.get("customer_city") or ""
    is_active = quote.get("is_active_customer")
    status_label = "active recurring service customer" if is_active else (
        quote.get("customer_status") or "unknown — may be a new or one-time customer"
    )
    total = float(quote.get("total_amount") or 0)

    lines = [
        "CUSTOMER:",
        f"  Name: {customer_name}",
        f"  First name (for scripts): {first_name}",
        f"  City: {city or 'unknown'}",
        f"  Customer status: {status_label}",
        "",
        "QUOTE:",
        f"  Quote number: #{quote.get('quote_number')}",
        f"  Status: {quote.get('status')}",
        f"  Quote date: {quote.get('quote_date') or 'unknown'}",
        f"  Sent date: {quote.get('sent_date') or 'unknown'}",
        f"  Expiration date: {quote.get('expiration_date') or 'none set'}",
        f"  Total: ${total:,.2f}",
        f"  Tax: ${float(quote.get('tax_amount') or 0):,.2f}",
        f"  Subtotal: ${float(quote.get('subtotal_amount') or 0):,.2f}",
    ]

    if quote.get("internal_notes"):
        lines += ["", f"INTERNAL NOTES FROM OFFICE/TECH:", f"  {quote['internal_notes']}"]

    if quote.get("message"):
        lines += ["", "CUSTOMER-FACING QUOTE MESSAGE (what the customer saw):", f"  {quote['message']}"]

    if items:
        lines += ["", f"LINE ITEMS ({len(items)} total):"]
        blank_desc_count = 0
        for i, item in enumerate(items, 1):
            name = (item.get("item_name") or "Unnamed item").strip()
            desc = (item.get("description") or "").strip()
            qty = float(item.get("quantity") or 1)
            unit = float(item.get("unit_price") or 0)
            line_total = qty * unit
            taxable = "taxable" if item.get("is_taxable") else "non-taxable"
            line = f"  {i}. {name}"
            if desc and desc.lower() != name.lower():
                line += f" — description: {desc}"
            else:
                blank_desc_count += 1
                line += " — [no description provided]"
            if qty != 1:
                line += f" | qty: {qty} × ${unit:,.2f} = ${line_total:,.2f}"
            else:
                line += f" | ${line_total:,.2f}"
            line += f" ({taxable})"
            lines.append(line)
        if blank_desc_count > 0:
            lines.append(f"  NOTE: {blank_desc_count} of {len(items)} line item(s) have no description. Infer carefully from item names but mark assumptions.")
    else:
        lines.append("")
        lines.append("LINE ITEMS: None available — the quote has no line items on file. Treat as unknown scope.")

    if pool_info:
        lines += ["", "POOL / LOCATION ON FILE:"]
        if pool_info.get("gallons"):
            lines.append(f"  Pool size: {pool_info['gallons']} gallons")
        if pool_info.get("baseline_filter_pressure"):
            lines.append(f"  Baseline filter pressure: {pool_info['baseline_filter_pressure']} PSI")
        equipment = pool_info.get("equipment_items")
        if equipment:
            if isinstance(equipment, str):
                try:
                    equipment = json.loads(equipment)
                except Exception:
                    equipment = None
            if isinstance(equipment, list) and equipment:
                lines.append("  Equipment on file:")
                for eq in equipment[:10]:
                    if isinstance(eq, dict):
                        brand = eq.get("Brand") or eq.get("brand") or ""
                        model = eq.get("Model") or eq.get("model") or ""
                        eq_type = eq.get("Type") or eq.get("type") or eq.get("EquipmentType") or ""
                        parts = [p for p in [brand, model, eq_type] if p]
                        if parts:
                            lines.append(f"    - {' '.join(parts)}")
        if pool_info.get("notes"):
            lines.append(f"  Pool notes: {pool_info['notes']}")
        if pool_info.get("sl_notes"):
            lines.append(f"  Location notes: {pool_info['sl_notes']}")

    if work_orders:
        lines += ["", "RECENT SERVICE HISTORY (most recent first):"]
        for wo in work_orders[:5]:
            date = _fmt_date(wo.get("service_date")) or "unknown date"
            wt = wo.get("work_order_type") or ""
            needed = (wo.get("work_needed") or "").strip()
            performed = (wo.get("work_performed") or "").strip()
            notes = (wo.get("notes") or "").strip()
            entry = f"  - {date}"
            if wt:
                entry += f" [{wt}]"
            if needed:
                entry += f" | Needed: {needed[:120]}"
            if performed:
                entry += f" | Done: {performed[:120]}"
            if notes:
                entry += f" | Notes: {notes[:100]}"
            lines.append(entry)
    else:
        lines += ["", "RECENT SERVICE HISTORY: None available."]

    lines += [
        "",
        "━━━ TASK ━━━",
        "Generate sales coaching notes for a salesperson calling this customer about this quote.",
        "Ground every section in the ACTUAL line items above.",
        "If line item descriptions are blank, infer from item names but flag your assumptions.",
        "Do NOT produce generic pool advice. Produce advice specific to what is in this quote.",
        "Return JSON only.",
    ]
    return "\n".join(lines)


def _quote_hash(quote: dict, items: list) -> str:
    payload = {
        "total_amount": str(quote.get("total_amount") or ""),
        "status": quote.get("status") or "",
        "items": [
            {
                "name": i.get("item_name") or "",
                "qty": str(i.get("quantity") or ""),
                "unit_price": str(i.get("unit_price") or ""),
            }
            for i in sorted(items, key=lambda x: x.get("sequence") or 0)
        ],
    }
    return hashlib.md5(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _call_openai(system: str, user: str) -> str:
    with httpx.Client(timeout=90.0) as client:
        resp = client.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": SALES_ASSIST_AI_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def get_ai_notes(source_quote_id: str) -> Optional[dict]:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, sales_notes_json, generated_at, stale_at,
                       error_message, ai_model, source_quote_hash
                FROM quote_ai_sales_notes
                WHERE source_quote_id = %s AND source_system = 'skimmer'
                """,
                (source_quote_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return dict(row)


def generate_ai_notes(source_quote_id: str) -> dict:
    if not OPENAI_API_KEY:
        return {"error": "OPENAI_API_KEY is not configured", "status": "error"}

    quote = get_quote_detail(source_quote_id)
    if not quote:
        return {"error": "Quote not found", "status": "error"}

    items = quote.get("line_items") or []
    pool_info = quote.get("pool_info")
    work_orders = quote.get("recent_work_orders") or []

    current_hash = _quote_hash(quote, items)

    # Check if cached result is still fresh for this exact quote data
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_quote_hash, status FROM quote_ai_sales_notes WHERE source_quote_id = %s AND source_system = 'skimmer'",
                (source_quote_id,),
            )
            cached = cur.fetchone()

    if cached and cached["source_quote_hash"] == current_hash and cached["status"] == "completed":
        return {"status": "already_current", "hash": current_hash}

    prompt = _build_quote_prompt(quote, items, pool_info, work_orders)

    # Mark as generating
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO quote_ai_sales_notes
                    (source_quote_id, source_system, source_quote_hash, ai_model, status)
                VALUES (%s, 'skimmer', %s, %s, 'generating')
                ON CONFLICT (source_system, source_quote_id) DO UPDATE SET
                    source_quote_hash = EXCLUDED.source_quote_hash,
                    ai_model = EXCLUDED.ai_model,
                    status = 'generating',
                    error_message = NULL,
                    updated_at = NOW()
                """,
                (source_quote_id, current_hash, SALES_ASSIST_AI_MODEL),
            )
        conn.commit()

    try:
        text = _call_openai(_AI_SYSTEM_PROMPT, prompt)
        notes_json = json.loads(text)

        with pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE quote_ai_sales_notes
                    SET status = 'completed',
                        sales_notes_json = %s,
                        generated_at = NOW(),
                        error_message = NULL,
                        updated_at = NOW()
                    WHERE source_quote_id = %s AND source_system = 'skimmer'
                    """,
                    (json.dumps(notes_json), source_quote_id),
                )
            conn.commit()

        return {"status": "completed", "notes": notes_json}

    except json.JSONDecodeError as exc:
        err = f"AI returned invalid JSON: {exc}"
        logger.error("sales_assist ai_notes json_error quote=%s: %s", source_quote_id, err)
        _mark_ai_error(source_quote_id, err)
        return {"status": "error", "error": err}
    except httpx.HTTPStatusError as exc:
        err = f"AI API HTTP error {exc.response.status_code}: {exc.response.text[:200]}"
        logger.error("sales_assist ai_notes http_error quote=%s: %s", source_quote_id, err)
        _mark_ai_error(source_quote_id, err)
        return {"status": "error", "error": err}
    except Exception as exc:
        err = str(exc)
        logger.error("sales_assist ai_notes error quote=%s: %s", source_quote_id, err)
        _mark_ai_error(source_quote_id, err)
        return {"status": "error", "error": err}


def _mark_ai_error(source_quote_id: str, error_message: str) -> None:
    try:
        with pg() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE quote_ai_sales_notes
                    SET status = 'error', error_message = %s, updated_at = NOW()
                    WHERE source_quote_id = %s AND source_system = 'skimmer'
                    """,
                    (error_message[:500], source_quote_id),
                )
            conn.commit()
    except Exception:
        pass


def mark_ai_notes_stale(source_quote_id: str) -> None:
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE quote_ai_sales_notes
                SET stale_at = NOW(), updated_at = NOW()
                WHERE source_quote_id = %s AND source_system = 'skimmer'
                """,
                (source_quote_id,),
            )
        conn.commit()
