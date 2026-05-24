"""Sales Assist service — open quote pipeline with AI-generated sales notes."""

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from pg import pg

logger = logging.getLogger("sales_assist")

SALES_ASSIST_AI_MODEL = os.getenv("SALES_ASSIST_AI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

_OPEN_STATUSES = ("Sent", "Draft")
_ALL_STATUSES = ("Sent", "Draft", "Expired", "Approved", "Rejected", "Archived")


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
    elif status == "expired":
        score += 8
        reasons.append("Quote expired — may be re-engageable")

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

    exp_date = quote.get("expiration_date")
    if exp_date:
        if isinstance(exp_date, str):
            try:
                exp_date = datetime.fromisoformat(exp_date.replace("Z", "+00:00"))
            except Exception:
                exp_date = None
        if exp_date:
            if exp_date.tzinfo is None:
                exp_date = exp_date.replace(tzinfo=timezone.utc)
            days_until_exp = (exp_date - now).days
            if 0 <= days_until_exp <= 7:
                score += 10
                reasons.append(f"Quote expires in {days_until_exp} day(s)")
            elif days_until_exp < 0:
                score += 3
                reasons.append(f"Quote expired {abs(days_until_exp)} day(s) ago")

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
        conditions.append("(q.customer_display_name ILIKE %s OR q.quote_number::text ILIKE %s)")
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
            q.customer_display_name,
            q.customer_city,
            q.customer_state,
            q.customer_zip,
            q.internal_notes,
            q.reject_reason,
            c.phone AS customer_phone,
            c.mobile_phone AS customer_mobile,
            c.email AS customer_email,
            c.customer_status,
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
                    c.phone AS customer_phone,
                    c.mobile_phone AS customer_mobile,
                    c.email AS customer_email,
                    c.customer_status,
                    c.first_name AS customer_first_name,
                    c.last_name AS customer_last_name,
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

_AI_SYSTEM_PROMPT = """You are a sales assistant for North Texas Pool Pros, a residential pool maintenance and repair company in the Dallas–Fort Worth area.
Your job is to help salespeople understand and effectively present quotes to customers when calling them.

RULES — follow these strictly:
- Do NOT invent model-specific equipment features unless the model is specified in the quote data
- Do NOT claim exact energy savings percentages unless supported by the quote data
- Do NOT claim warranty terms unless the quote data specifies them
- Do NOT claim rebates or incentives unless confirmed in the quote data
- Do NOT say equipment is unsafe unless source data explicitly supports that conclusion
- Clearly distinguish between confirmed facts from the quote and general pool industry talking points
- Keep tone professional, helpful, and consultative — never high-pressure
- If model or SKU is missing, note that model-specific benefits should be confirmed before quoting them
- Do NOT pressure customers; the goal is to help them understand value

Return ONLY a valid JSON object matching this schema exactly — no markdown, no explanation:
{
  "summary": "string — 2-3 sentence plain-English description of what this quote covers",
  "problem_solved": "string — what customer problem or need this quote addresses",
  "confirmed_quote_facts": ["string", ...],
  "general_talking_points": ["string", ...],
  "efficiency_points": ["string", ...],
  "reliability_points": ["string", ...],
  "comfort_convenience_points": ["string", ...],
  "risks_of_waiting": ["string", ...],
  "likely_objections": [{"objection": "string", "suggested_response": "string"}, ...],
  "call_opening": "string — suggested opening line for the sales call",
  "closing_script": "string — suggested close to the call",
  "sms_follow_up": "string — suggested SMS message to send after the call",
  "email_follow_up": "string — suggested follow-up email body (short)",
  "questions_to_ask": ["string", ...],
  "claims_to_avoid": ["string — things NOT to say unless you can confirm them"],
  "confidence": "high|medium|low",
  "missing_information": ["string — data that would make these notes more accurate"]
}"""


def _build_quote_prompt(quote: dict, items: list, pool_info: Optional[dict], work_orders: list) -> str:
    customer_name = quote.get("customer_display_name") or "Unknown Customer"
    city = quote.get("customer_city") or ""
    status_label = "active recurring service customer" if quote.get("is_active_customer") else (
        quote.get("customer_status") or "unknown status"
    )

    phone = quote.get("customer_phone") or quote.get("customer_mobile") or ""
    total = float(quote.get("total_amount") or 0)

    lines = [
        f"CUSTOMER INFORMATION:",
        f"Name: {customer_name}",
        f"City: {city}",
        f"Customer Status: {status_label}",
    ]
    if phone:
        lines.append(f"Phone: [redacted for AI]")

    lines += [
        "",
        f"QUOTE DETAILS:",
        f"Quote Number: #{quote.get('quote_number')}",
        f"Status: {quote.get('status')}",
        f"Quote Date: {quote.get('quote_date') or 'Unknown'}",
        f"Expiration Date: {quote.get('expiration_date') or 'Not specified'}",
        f"Total: ${total:,.2f}",
    ]

    if quote.get("message"):
        lines += ["", f"CUSTOMER-FACING MESSAGE:", quote["message"]]

    if quote.get("internal_notes"):
        lines += ["", f"INTERNAL NOTES:", quote["internal_notes"]]

    if items:
        lines += ["", "LINE ITEMS:"]
        for i, item in enumerate(items, 1):
            name = item.get("item_name") or "Item"
            desc = item.get("description") or ""
            qty = item.get("quantity") or 1
            price = float(item.get("total_price") or item.get("unit_price") or 0)
            line = f"{i}. {name}"
            if desc:
                line += f" — {desc}"
            line += f" (qty {qty}, ${price:,.2f})"
            lines.append(line)

    if pool_info:
        lines += ["", "POOL INFORMATION:"]
        if pool_info.get("gallons"):
            lines.append(f"Pool size: {pool_info['gallons']} gallons")
        if pool_info.get("name"):
            lines.append(f"Pool name: {pool_info['name']}")
        equipment = pool_info.get("equipment_items")
        if equipment:
            if isinstance(equipment, str):
                try:
                    equipment = json.loads(equipment)
                except Exception:
                    pass
            if isinstance(equipment, list) and equipment:
                lines.append("Equipment on file:")
                for eq in equipment[:8]:
                    if isinstance(eq, dict):
                        brand = eq.get("Brand") or eq.get("brand") or ""
                        model = eq.get("Model") or eq.get("model") or ""
                        eq_type = eq.get("Type") or eq.get("type") or eq.get("EquipmentType") or ""
                        parts = [p for p in [brand, model, eq_type] if p]
                        if parts:
                            lines.append(f"  - {' '.join(parts)}")
        if pool_info.get("notes"):
            lines.append(f"Pool notes: {pool_info['notes']}")
        if pool_info.get("sl_notes"):
            lines.append(f"Location notes: {pool_info['sl_notes']}")

    if work_orders:
        lines += ["", "RECENT SERVICE HISTORY (most recent first):"]
        for wo in work_orders[:5]:
            date = _fmt_date(wo.get("service_date")) or "Unknown date"
            wt = wo.get("work_order_type") or ""
            needed = wo.get("work_needed") or ""
            performed = wo.get("work_performed") or ""
            notes = wo.get("notes") or ""
            entry = f"- {date}"
            if wt:
                entry += f" [{wt}]"
            if needed:
                entry += f": Needed — {needed[:100]}"
            if performed:
                entry += f"; Done — {performed[:100]}"
            if notes:
                entry += f"; Notes — {notes[:80]}"
            lines.append(entry)

    lines += ["", "Generate sales notes as JSON per the schema provided."]
    return "\n".join(lines)


def _quote_hash(quote: dict, items: list) -> str:
    payload = {
        "total_amount": str(quote.get("total_amount") or ""),
        "status": quote.get("status") or "",
        "items": [
            {
                "name": i.get("item_name") or "",
                "qty": str(i.get("quantity") or ""),
                "price": str(i.get("total_price") or ""),
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
