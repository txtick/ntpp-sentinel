"""
Chemical invoice ingestion service.

Extraction modes
  stub   — returns a hard-coded fixture (tests / smoke-testing only; no PDF parsing)
  text   — pdfplumber text extraction + Heritage Pool Supply regex parser
  openai — pdfplumber text extraction + OpenAI chat completions for structured JSON

Reconciliation is always deterministic math, regardless of extraction mode.
No record is silently accepted on a math mismatch.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from db import db


# ─── Normalization dictionary ─────────────────────────────────────────────────
# Seeded from the Heritage Pool Supply ruleset.  Match order matters: first hit wins.

NORMALIZATION_RULES: List[Dict[str, Any]] = [
    {
        "match_contains": ["POOL BREEZE GRANULAR", "CAL HYPO", "CALCIUM HYPOCHLORITE"],
        "normalized_product_name": "calcium hypochlorite",
        "category": "shock",
        "cost_type": "chemical",
        "default_unit": "lb",
    },
    {
        "match_contains": [
            '3" CHLORINATING TABLETS',
            "3 CHLORINATING TABLETS",
            "CHLORINATING TABLETS",
            "TRICHLOR",
            "TABS",
        ],
        "normalized_product_name": "chlorine tablets",
        "category": "tabs",
        "cost_type": "chemical",
        "default_unit": "lb",
    },
    {
        "match_contains": ["MURIATIC ACID", "HASA MURIATIC ACID"],
        "normalized_product_name": "muriatic acid",
        "category": "acid",
        "cost_type": "chemical",
        "default_unit": "gal",
    },
    {
        "match_contains": ["SODIUM BICARBONATE", "BICARBONATE"],
        "normalized_product_name": "sodium bicarbonate",
        "category": "alkalinity",
        "cost_type": "chemical",
        "default_unit": "lb",
    },
    {
        "match_contains": ["AQUASALT", "POOL SALT", "SALT BAG"],
        "normalized_product_name": "pool salt",
        "category": "salt",
        "cost_type": "chemical",
        "default_unit": "lb",
    },
    {
        "match_contains": ["PHOSFIGHT", "PHOSPHATE REMOVER"],
        "normalized_product_name": "phosphate remover",
        "category": "phosphate_remover",
        "cost_type": "chemical",
        "default_unit": "quart",
    },
    {
        "match_contains": ["STRIKE-OUT ALGAECIDE", "ALGAECIDE"],
        "normalized_product_name": "algaecide",
        "category": "algaecide",
        "cost_type": "chemical",
        "default_unit": "quart",
    },
    {
        "match_contains": ["DIATOMACEOUS EARTH"],
        "normalized_product_name": "diatomaceous earth",
        "category": "filter_media",
        "cost_type": "chemical",
        "default_unit": "lb",
    },
    {
        "match_contains": [
            "TAYLOR",
            "THIOSULF",
            "SULF ACID",
            "CYANURIC ACID REAGENT",
            "REAGENT",
        ],
        "normalized_product_name": "testing reagent",
        "category": "reagent/testing",
        "cost_type": "testing",
        "default_unit": "each",
    },
    {
        "match_contains": ["PENTAIR BLENDED", "NYLON BRUSH", "BRUSH"],
        "normalized_product_name": "pool brush",
        "category": "equipment_part",
        "cost_type": "equipment",
        "default_unit": "each",
    },
]

INVOICE_STATUSES = {"PENDING", "RECONCILED", "REVIEW_NEEDED", "REVIEWED", "DUPLICATE"}
ALIAS_MAPPING_STATUSES = {"APPROVED", "AI_SUGGESTED", "NEEDS_REVIEW", "REJECTED"}
ALIAS_COST_TYPES = {"chemical", "equipment", "testing", "misc"}
ALIAS_CATEGORIES = {
    *(rule["category"] for rule in NORMALIZATION_RULES),
    "other",
    "custom",
    "stabilizer",
    "clarifier",
}
FAILED_SCAN_STATUSES = {"ERROR", "EXTRACTION_FAILED", "PDF_TOO_LARGE"}
CHEM_INVOICE_AI_CLASSIFICATION_ENABLED = os.getenv("CHEM_INVOICE_AI_CLASSIFICATION_ENABLED", "1").lower() in ("1", "true", "yes", "on")


def _env_flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes", "on")


def clamp_limit_offset(limit: int, offset: int, *, max_limit: int = 500) -> Tuple[int, int]:
    return max(1, min(int(limit), max_limit)), max(0, int(offset))


def validate_invoice_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    normalized = str(status).strip().upper()
    if not normalized:
        return None
    if normalized not in INVOICE_STATUSES:
        raise ValueError(f"Invalid invoice status: {status}")
    return normalized


def validate_alias_status(status: Optional[str]) -> Optional[str]:
    if status is None:
        return None
    normalized = str(status).strip().upper()
    if not normalized:
        return None
    if normalized not in ALIAS_MAPPING_STATUSES:
        raise ValueError(f"Invalid alias status: {status}")
    return normalized


def validate_summary_dates(start_date: Optional[str], end_date: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    parsed_start = dt.date.fromisoformat(start_date) if start_date else None
    parsed_end = dt.date.fromisoformat(end_date) if end_date else None
    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise ValueError("start_date must be on or before end_date")
    return (
        parsed_start.isoformat() if parsed_start else None,
        parsed_end.isoformat() if parsed_end else None,
    )


def _clean_slug_part(value: Optional[str], fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return cleaned[:64] or fallback


def build_safe_pdf_name(vendor_name: Optional[str], invoice_number: Optional[str], sha256: str) -> str:
    vendor_slug = _clean_slug_part(vendor_name, "unknown_vendor")
    invoice_slug = _clean_slug_part(invoice_number, "unknown_invoice")
    return f"{vendor_slug}_{invoice_slug}_{sha256[:8]}.pdf"


def unique_dest_path(dest_dir: str, filename: str) -> str:
    base = Path(dest_dir) / filename
    if not base.exists():
        return str(base)
    stem = base.stem
    suffix = base.suffix
    counter = 2
    while True:
        candidate = base.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return str(candidate)
        counter += 1


def validate_alias_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "normalized_product_name",
        "category",
        "cost_type",
        "default_unit",
        "package_size",
        "confidence",
        "mapping_status",
    }
    cleaned = {k: v for k, v in updates.items() if k in allowed}
    if "category" in cleaned:
        category = str(cleaned["category"]).strip()
        if category not in ALIAS_CATEGORIES:
            raise ValueError(f"Invalid category: {cleaned['category']}")
        cleaned["category"] = category
    if "cost_type" in cleaned:
        cost_type = str(cleaned["cost_type"]).strip()
        if cost_type not in ALIAS_COST_TYPES:
            raise ValueError(f"Invalid cost_type: {cleaned['cost_type']}")
        cleaned["cost_type"] = cost_type
    if "mapping_status" in cleaned:
        cleaned["mapping_status"] = validate_alias_status(str(cleaned["mapping_status"]))
    if "normalized_product_name" in cleaned:
        cleaned["normalized_product_name"] = str(cleaned["normalized_product_name"]).strip()
    if "default_unit" in cleaned:
        cleaned["default_unit"] = str(cleaned["default_unit"]).strip()
    if "package_size" in cleaned and cleaned["package_size"] is not None:
        cleaned["package_size"] = str(cleaned["package_size"]).strip()
    if "confidence" in cleaned:
        cleaned["confidence"] = float(cleaned["confidence"])
    return {k: v for k, v in cleaned.items() if v not in ("", None)}


def match_normalization_rule(raw_description: str) -> Optional[Dict[str, Any]]:
    upper = raw_description.upper()
    for rule in NORMALIZATION_RULES:
        if any(tok.upper() in upper for tok in rule["match_contains"]):
            return rule
    return None


# ─── Product alias DB helpers ─────────────────────────────────────────────────


def _lookup_alias(
    conn: sqlite3.Connection,
    vendor_name: str,
    item_code: Optional[str],
    raw_description: str,
) -> Optional[sqlite3.Row]:
    """Return the best matching alias row, preferring APPROVED > AI_SUGGESTED."""
    if item_code:
        row = conn.execute(
            """
            SELECT * FROM chemical_product_aliases
            WHERE vendor_name=? AND item_code=? AND mapping_status != 'REJECTED'
            ORDER BY CASE mapping_status
                WHEN 'APPROVED' THEN 0
                WHEN 'AI_SUGGESTED' THEN 1
                ELSE 2
            END
            LIMIT 1
            """,
            (vendor_name, item_code),
        ).fetchone()
        if row:
            return row

    # Fall back to substring match against stored pattern
    rows = conn.execute(
        """
        SELECT * FROM chemical_product_aliases
        WHERE vendor_name=? AND mapping_status IN ('APPROVED','AI_SUGGESTED')
        """,
        (vendor_name,),
    ).fetchall()
    upper = raw_description.upper()
    for row in rows:
        pattern = (row["raw_description_pattern"] or "").upper()
        if pattern and pattern in upper:
            return row
    return None


def _upsert_alias(
    conn: sqlite3.Connection,
    vendor_name: str,
    item_code: Optional[str],
    raw_description: str,
    normalized_product_name: str,
    category: str,
    cost_type: str,
    default_unit: str,
    confidence: float,
    mapping_status: str,
    package_size: Optional[str] = None,
) -> int:
    """Insert or update a chemical_product_aliases row. Returns the alias id."""
    now = dt.datetime.utcnow().isoformat()
    pattern = raw_description[:200]

    existing: Optional[sqlite3.Row] = None
    if item_code:
        existing = conn.execute(
            "SELECT * FROM chemical_product_aliases WHERE vendor_name=? AND item_code=?",
            (vendor_name, item_code),
        ).fetchone()
    if existing is None:
        existing = conn.execute(
            "SELECT * FROM chemical_product_aliases WHERE vendor_name=? AND raw_description_pattern=?",
            (vendor_name, pattern),
        ).fetchone()

    if existing:
        alias_id = existing["id"]
        if existing["mapping_status"] == "APPROVED":
            # Never downgrade an approved mapping — just bump the seen counter
            conn.execute(
                "UPDATE chemical_product_aliases SET last_seen_at=?, times_seen=times_seen+1 WHERE id=?",
                (now, alias_id),
            )
        else:
            conn.execute(
                """
                UPDATE chemical_product_aliases SET
                    normalized_product_name=?, category=?, cost_type=?,
                    default_unit=?, mapping_status=?, confidence=?,
                    last_seen_at=?, times_seen=times_seen+1,
                    package_size=COALESCE(?, package_size)
                WHERE id=?
                """,
                (
                    normalized_product_name, category, cost_type,
                    default_unit, mapping_status, confidence,
                    now, package_size, alias_id,
                ),
            )
        return alias_id

    cur = conn.execute(
        """
        INSERT INTO chemical_product_aliases
            (vendor_name, item_code, raw_description_pattern,
             normalized_product_name, category, cost_type, default_unit,
             package_size, mapping_status, confidence,
             first_seen_at, last_seen_at, times_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            vendor_name, item_code, pattern,
            normalized_product_name, category, cost_type, default_unit,
            package_size, mapping_status, confidence,
            now, now,
        ),
    )
    return cur.lastrowid  # type: ignore[return-value]


def normalize_line(
    conn: sqlite3.Connection,
    vendor_name: str,
    item_code: Optional[str],
    raw_description: str,
    ai_suggestion: Optional[Dict[str, Any]] = None,
    *,
    allow_ai_classification: bool = False,
    openai_api_key: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    openai_model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Classify a single invoice line item.

    Priority:
      1. APPROVED alias in DB (admin-confirmed truth)
      2. Normalization dictionary match (seeded rules, marked APPROVED)
      3. AI suggestion — if confidence >= 0.90: AI_SUGGESTED; else NEEDS_REVIEW / "other"
      4. Hard fallback: category="other", cost_type="misc", confidence=0.0
    """
    # 1. Approved alias
    alias_row = _lookup_alias(conn, vendor_name, item_code, raw_description)
    if alias_row and alias_row["mapping_status"] == "APPROVED":
        _upsert_alias(
            conn, vendor_name, item_code, raw_description,
            alias_row["normalized_product_name"], alias_row["category"],
            alias_row["cost_type"], alias_row["default_unit"],
            float(alias_row["confidence"] or 1.0), "APPROVED",
        )
        return {
            "normalized_product_name": alias_row["normalized_product_name"],
            "category": alias_row["category"],
            "cost_type": alias_row["cost_type"],
            "default_unit": alias_row["default_unit"],
            "confidence": 1.0,
            "alias_id": alias_row["id"],
        }

    # 2. Dictionary match
    rule = match_normalization_rule(raw_description)
    if rule:
        alias_id = _upsert_alias(
            conn, vendor_name, item_code, raw_description,
            rule["normalized_product_name"], rule["category"],
            rule["cost_type"], rule["default_unit"],
            confidence=0.98, mapping_status="APPROVED",
        )
        return {
            "normalized_product_name": rule["normalized_product_name"],
            "category": rule["category"],
            "cost_type": rule["cost_type"],
            "default_unit": rule["default_unit"],
            "confidence": 0.98,
            "alias_id": alias_id,
        }

    # 3. AI suggestion
    if ai_suggestion is None and allow_ai_classification:
        try:
            ai_suggestion = classify_product_with_ai(
                raw_description,
                openai_api_key=openai_api_key,
                openai_base_url=openai_base_url,
                model=openai_model,
            )
        except Exception:
            ai_suggestion = None

    if ai_suggestion:
        conf = float(ai_suggestion.get("confidence", 0.0))
        if conf >= 0.90:
            status = "AI_SUGGESTED"
            cat = str(ai_suggestion.get("category", "other"))
            ct = str(ai_suggestion.get("cost_type", "misc"))
            norm = str(ai_suggestion.get("normalized_product_name", "unknown"))
        else:
            status = "NEEDS_REVIEW"
            cat = "other"
            ct = "misc"
            norm = "unknown"
        if cat not in ALIAS_CATEGORIES:
            cat = "other"
        if ct not in ALIAS_COST_TYPES:
            ct = "misc"
        du = str(ai_suggestion.get("default_unit", "each"))
        alias_id = _upsert_alias(
            conn, vendor_name, item_code, raw_description,
            norm, cat, ct, du,
            confidence=conf, mapping_status=status,
            package_size=ai_suggestion.get("package_size"),
        )
        return {
            "normalized_product_name": norm,
            "category": cat,
            "cost_type": ct,
            "default_unit": du,
            "confidence": conf,
            "alias_id": alias_id,
        }

    # 4. Unknown fallback
    alias_id = _upsert_alias(
        conn, vendor_name, item_code, raw_description,
        "unknown", "other", "misc", "each",
        confidence=0.0, mapping_status="NEEDS_REVIEW",
    )
    return {
        "normalized_product_name": "unknown",
        "category": "other",
        "cost_type": "misc",
        "default_unit": "each",
        "confidence": 0.0,
        "alias_id": alias_id,
    }


# ─── Reconciliation ───────────────────────────────────────────────────────────


def reconcile(
    line_items: List[Dict[str, Any]],
    subtotal: Optional[float],
    tax: Optional[float],
    fees: Optional[float],
    invoice_total: Optional[float],
    tolerance: float = 0.02,
) -> Dict[str, Any]:
    """
    Pure math check.  Returns {"status": ..., "reconciliation": {...}}.

    line_sum must equal subtotal (within tolerance).
    subtotal + tax + fees must equal invoice_total (within tolerance).
    Both conditions must hold for RECONCILED; otherwise REVIEW_NEEDED.
    """
    line_sum = round(sum(float(i.get("extended_price") or 0) for i in line_items), 2)
    has_subtotal = subtotal is not None
    has_invoice_total = invoice_total is not None
    sub = float(subtotal or 0)
    tx = float(tax or 0)
    fe = float(fees or 0)
    calc_total = round(sub + tx + fe, 2)
    inv_total = float(invoice_total or 0)

    ok_sub = has_subtotal and round(abs(line_sum - sub), 4) <= tolerance
    ok_total = has_invoice_total and round(abs(calc_total - inv_total), 4) <= tolerance

    status = "RECONCILED" if (ok_sub and ok_total) else "REVIEW_NEEDED"
    return {
        "status": status,
        "reconciliation": {
            "line_sum": line_sum,
            "subtotal": sub,
            "tax": tx,
            "fees": fe,
            "calculated_total": calc_total,
            "invoice_total": inv_total,
            "tolerance": tolerance,
            "matches_subtotal": ok_sub,
            "matches_invoice_total": ok_total,
            "has_subtotal": has_subtotal,
            "has_invoice_total": has_invoice_total,
        },
    }


# ─── PDF text extraction ──────────────────────────────────────────────────────


def pdf_sha256(pdf_bytes: bytes) -> str:
    return hashlib.sha256(pdf_bytes).hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        raise RuntimeError(
            "pdfplumber is not installed. Set CHEM_INVOICE_EXTRACTOR_MODE=stub "
            "or install app/requirements.txt locally."
        )
    pages: List[str] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


# ─── Heritage Pool Supply text parser ────────────────────────────────────────
#
# Format notes (derived from sample invoices):
#   • Multi-column table: QTY ORDERED | QTY SHIPPED | UOM | ITEM/DESCRIPTION
#                         | CONVERTED QTY | PRICE/UOM | EXTENDED AMOUNT
#   • Item codes: uppercase letters + digits (e.g. PBZ88478)
#   • Footer pattern: ******SUB-TOTAL****** <amount>
#                     Sales Tax <pct>% <amount>
#                     BALANCE $<amount>
#
# When pdfplumber reads this layout the columns may be interleaved.  We extract
# header scalars with regex on the full text, then identify line items by
# locating item-code tokens and walking forward to collect description and price.

_INVOICE_NUM_RE = re.compile(r"Invoice\s*#\s*:\s*(\S+)", re.IGNORECASE)
_INVOICE_DATE_RE = re.compile(r"Invoice\s+Date\s*:\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE)
_ACCOUNT_RE = re.compile(r"Account\s*#\s*:\s*(\S+)", re.IGNORECASE)
_PO_RE = re.compile(r"PO\s+NUMBER\s*\n?\s*(\S+)", re.IGNORECASE)
_BRANCH_RE = re.compile(r"HERITAGE POOL SUPPLY\s+([\w\s]+?)(?:\n|$)", re.IGNORECASE)
_SUBTOTAL_RE = re.compile(r"\*+\s*SUB-TOTAL\s*\*+\s+([\d,]+\.\d{2})", re.IGNORECASE)
_TAX_RE = re.compile(r"Sales\s+Tax\s+[\d.]+%\s+([\d,]+\.\d{2})", re.IGNORECASE)
_BALANCE_RE = re.compile(r"BALANCE\s+\$?([\d,]+\.\d{2})", re.IGNORECASE)

# Item code: 2+ uppercase letters followed by digits (no spaces)
_ITEM_CODE_RE = re.compile(r"\b([A-Z]{2,}[0-9]+[A-Z0-9]*)\b")
# Converted-qty + unit-price + extended-price trailing line
# e.g. "1.00 /EA  205.00 /EA  205.00"
_PRICE_TRAIL_RE = re.compile(
    r"(\d[\d,]*\.\d{2})\s*/\s*\w+\s+"
    r"(\d[\d,]*\.\d{2})\s*/\s*\w+\s+"
    r"(\d[\d,]*\.\d{2})"
)
# Or sometimes just "unit_price /UOM  extended" (two numbers, not three)
_PRICE_TRAIL2_RE = re.compile(
    r"(\d[\d,]*\.\d{2})\s*/\s*\w+\s+"
    r"(\d[\d,]*\.\d{2})(?:\s*$)"
)


def _pf(s: str) -> float:
    return float(s.replace(",", ""))


def parse_heritage_pool_invoice(text: str) -> Dict[str, Any]:
    """Best-effort parse.  Returns structured dict; caller reconciles the math."""
    vendor_name = "Heritage Pool Supply"

    m = _BRANCH_RE.search(text)
    branch_location = m.group(1).strip() if m else None

    m = _INVOICE_NUM_RE.search(text)
    invoice_number = m.group(1).strip() if m else None

    invoice_date: Optional[str] = None
    m = _INVOICE_DATE_RE.search(text)
    if m:
        raw = m.group(1).strip()
        parts = raw.split("/")
        if len(parts) == 3:
            mo, dy, yr = parts
            yr = "20" + yr if len(yr) == 2 else yr
            invoice_date = f"{yr}-{mo.zfill(2)}-{dy.zfill(2)}"

    m = _ACCOUNT_RE.search(text)
    account_number = m.group(1).strip() if m else None

    m = _PO_RE.search(text)
    po_number = m.group(1).strip() if m else None

    m = _SUBTOTAL_RE.search(text)
    subtotal: Optional[float] = _pf(m.group(1)) if m else None

    m = _TAX_RE.search(text)
    tax: Optional[float] = _pf(m.group(1)) if m else None

    m = _BALANCE_RE.search(text)
    invoice_total: Optional[float] = _pf(m.group(1)) if m else None

    # ── Line item extraction ──────────────────────────────────────────────────
    # Strategy: find every item-code position in the text, then collect the
    # description lines between that code and the next code (or footer), and
    # look for the price pattern in that block.
    lines = text.splitlines()
    n = len(lines)

    # Build a list of (line_index, item_code) for every item code found
    item_positions: List[Tuple[int, str]] = []
    seen_codes: set = set()
    for idx, line in enumerate(lines):
        for m_code in _ITEM_CODE_RE.finditer(line):
            code = m_code.group(1)
            # Filter out codes that are clearly not product codes:
            # account numbers, invoice numbers, EPA numbers, branch codes
            if code in seen_codes:
                continue
            if any(skip in line.upper() for skip in ("INVOICE", "ACCOUNT", "EPA NO", "BRANCH", "DELIVERY")):
                continue
            seen_codes.add(code)
            item_positions.append((idx, code))

    line_items: List[Dict[str, Any]] = []
    for pos_idx, (start_line, item_code) in enumerate(item_positions):
        # Collect lines from start_line up to the next item code (or footer)
        if pos_idx + 1 < len(item_positions):
            end_line = item_positions[pos_idx + 1][0]
        else:
            # End at footer markers
            end_line = n
            for k in range(start_line, n):
                if _SUBTOTAL_RE.search(lines[k]) or _BALANCE_RE.search(lines[k]):
                    end_line = k
                    break

        block = lines[start_line:end_line]
        block_text = " ".join(line.strip() for line in block if line.strip())

        # Extract description: everything between the item code and the price trail
        pm = _PRICE_TRAIL_RE.search(block_text)
        pm2 = _PRICE_TRAIL2_RE.search(block_text) if not pm else None
        price_match = pm or pm2

        if pm:
            unit_price = _pf(pm.group(2))
            extended_price = _pf(pm.group(3))
        elif pm2:
            unit_price = _pf(pm2.group(1))
            extended_price = _pf(pm2.group(2))
        else:
            unit_price = None
            extended_price = None

        # Raw description: item_code + cleaned description lines, minus EPA/HAZMAT noise
        desc_lines = []
        for bl in block:
            stripped = bl.strip()
            if not stripped:
                continue
            if stripped.startswith("EPA NO"):
                continue
            if _PRICE_TRAIL_RE.search(stripped) or _PRICE_TRAIL2_RE.search(stripped):
                continue
            desc_lines.append(stripped)
        raw_description = " ".join(desc_lines).strip()
        if not raw_description:
            raw_description = item_code

        # Quantity: look for leading digit before UOM on the first line of the block
        qty: float = 1.0
        uom_m = re.search(r"\b(\d+)\s+(EA|CS|BAG|GAL|LB|QT|EACH|CASE|QUART)\b", lines[start_line], re.IGNORECASE)
        if uom_m:
            qty = float(uom_m.group(1))
            unit = uom_m.group(2).lower()
        else:
            unit = "each"

        line_items.append({
            "line_number": len(line_items) + 1,
            "item_code": item_code,
            "raw_description": raw_description,
            "quantity": qty,
            "unit": unit,
            "unit_price": unit_price,
            "extended_price": extended_price,
        })

    return {
        "vendor_name": vendor_name,
        "branch_location": branch_location,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "account_number": account_number,
        "po_number": po_number,
        "ordered_by": None,
        "invoice_total": invoice_total,
        "subtotal": subtotal,
        "tax": tax,
        "fees": 0.0,
        "line_items": line_items,
    }


# ─── OpenAI extractor ─────────────────────────────────────────────────────────
#
# Uses httpx (same pattern as ai_gate.py) — no openai SDK dependency.
# The model acts purely as a data-entry clerk; all math is re-validated
# deterministically by the reconcile() function below.

_OPENAI_SYSTEM = """\
You are a precise data-entry assistant. Extract invoice data from the provided \
text and return ONLY a valid JSON object — no markdown, no explanation, no extra \
keys.  Do not invent values; use null for any field you cannot read clearly.
"""

_OPENAI_SCHEMA_PROMPT = """\
Return this exact JSON structure:

{
  "vendor_name": "string",
  "branch_location": "string or null",
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "account_number": "string or null",
  "po_number": "string or null",
  "ordered_by": "string or null",
  "invoice_total": number_or_null,
  "subtotal": number_or_null,
  "tax": number_or_null,
  "fees": 0.0,
  "line_items": [
    {
      "line_number": integer,
      "item_code": "string or null",
      "raw_description": "string",
      "quantity": number,
      "unit": "string",
      "unit_price": number_or_null,
      "extended_price": number,
      "ai_suggestion": {
        "normalized_product_name": "string",
        "category": "string",
        "cost_type": "chemical|testing|equipment|misc",
        "default_unit": "string",
        "package_size": "string or null",
        "confidence": 0.0_to_1.0
      }
    }
  ]
}

Rules:
- vendor_name: the supplier name (e.g. "Heritage Pool Supply")
- branch_location: city/location of the branch (e.g. "Denton, TX")
- invoice_date must be YYYY-MM-DD; convert from MM/DD/YY if needed
- invoice_total is the BALANCE due
- subtotal is the pre-tax line-item total
- For each line item, raw_description must capture the full description text
- quantity is the qty_shipped (not ordered)
- unit is the UOM code (EA, CS, BAG, etc.) in lowercase
- extended_price is qty × unit_price (the rightmost column dollar amount)
- In ai_suggestion, be honest about confidence; use 0.0 if uncertain
"""

_OPENAI_CLASSIFY_SYSTEM = """\
You classify pool-supply invoice line items. Return ONLY valid JSON with no markdown.
Be conservative. If uncertain, lower confidence.
"""

_OPENAI_CLASSIFY_PROMPT = """\
Return this exact JSON structure:

{
  "normalized_product_name": "string",
  "category": "string",
  "cost_type": "chemical|testing|equipment|misc",
  "confidence": 0.0_to_1.0
}

Rules:
- Classify only the provided raw invoice line description
- Use short normalized product names
- If uncertain, use category "other", cost_type "misc", and a lower confidence
"""


def classify_product_with_ai(
    raw_description: str,
    *,
    openai_api_key: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    import httpx  # type: ignore

    api_key = openai_api_key if openai_api_key is not None else os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set; cannot classify unknown invoice products")

    base_url = (openai_base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
    resolved_model = model or os.getenv("CHEM_INVOICE_OPENAI_MODEL", "gpt-4o-mini")
    prompt = f"{_OPENAI_CLASSIFY_PROMPT}\n\nRaw description:\n{raw_description[:1000]}"

    with httpx.Client(timeout=timeout) as client:
        resp = client.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": resolved_model,
                "messages": [
                    {"role": "system", "content": _OPENAI_CLASSIFY_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 200,
            },
        )
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    content = re.sub(r"```\s*$", "", content.strip())
    payload = json.loads(content)
    return {
        "normalized_product_name": str(payload.get("normalized_product_name", "unknown")).strip() or "unknown",
        "category": str(payload.get("category", "other")).strip() or "other",
        "cost_type": str(payload.get("cost_type", "misc")).strip() or "misc",
        "confidence": float(payload.get("confidence", 0.0) or 0.0),
    }


async def extract_via_openai(
    pdf_text: str,
    openai_api_key: str,
    openai_base_url: str,
    model: str,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    import httpx  # type: ignore

    if not openai_api_key:
        raise ValueError("OPENAI_API_KEY is not set; cannot use openai extraction mode")

    prompt = f"{_OPENAI_SCHEMA_PROMPT}\n\nInvoice text:\n{pdf_text[:12000]}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{openai_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _OPENAI_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 2000,
            },
        )
        resp.raise_for_status()

    content = resp.json()["choices"][0]["message"]["content"]

    # Strip markdown fences if the model wraps its JSON
    content = re.sub(r"^```(?:json)?\s*", "", content.strip(), flags=re.IGNORECASE)
    content = re.sub(r"```\s*$", "", content.strip())

    return json.loads(content)


# ─── Stub extractor (tests / smoke) ──────────────────────────────────────────

def _stub_extraction() -> Dict[str, Any]:
    """Returns a fixed Heritage Pool Supply invoice fixture."""
    return {
        "vendor_name": "Heritage Pool Supply",
        "branch_location": "Denton, TX",
        "invoice_number": "0000000000-STUB",
        "invoice_date": "2026-04-23",
        "account_number": "H052939",
        "po_number": "john",
        "ordered_by": None,
        "invoice_total": 489.55,
        "subtotal": 452.23,
        "tax": 37.32,
        "fees": 0.0,
        "line_items": [
            {
                "line_number": 1,
                "item_code": "PBZ88478",
                "raw_description": "PBZ88478 POOL BREEZE GRANULAR 68 CAL HYPO HAZMAT 100 LB",
                "quantity": 1.0,
                "unit": "ea",
                "unit_price": 205.00,
                "extended_price": 205.00,
            },
            {
                "line_number": 2,
                "item_code": "PBZ88413",
                "raw_description": "PBZ88413 POOL BREEZE 3\" CHLORINATING TABLETS HAZMAT 50 LB",
                "quantity": 1.0,
                "unit": "ea",
                "unit_price": 145.00,
                "extended_price": 145.00,
            },
            {
                "line_number": 3,
                "item_code": "BKISC50",
                "raw_description": "BKISC50 SODIUM BICARBONATE NSP POOL GRADE 50 LB",
                "quantity": 2.0,
                "unit": "ea",
                "unit_price": 20.63,
                "extended_price": 41.26,
            },
            {
                "line_number": 4,
                "item_code": "LZA71271",
                "raw_description": "LZA71271 GLB PHOSFIGHT PLUS PHOSPHATE REMOVER 1 QUART",
                "quantity": 1.0,
                "unit": "ea",
                "unit_price": 22.07,
                "extended_price": 22.07,
            },
            {
                "line_number": 5,
                "item_code": "PENR111358",
                "raw_description": "PENR111358 PENTAIR BLENDED STAINLESS/NYLON BRUSH #907 WHITE BRISTLES 18\"",
                "quantity": 1.0,
                "unit": "ea",
                "unit_price": 38.90,
                "extended_price": 38.90,
            },
        ],
    }


# ─── Core ingest pipeline ─────────────────────────────────────────────────────


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return dict(row)


def ingest_pdf_bytes(
    pdf_bytes: bytes,
    mode: str,
    source_path: Optional[str] = None,
    openai_api_key: str = "",
    openai_base_url: str = "https://api.openai.com/v1",
    openai_model: str = "gpt-4o-mini",
    reconcile_tolerance: float = 0.02,
    max_pdf_bytes: Optional[int] = None,
    # Synchronous callers pass a pre-extracted dict for testing
    _extracted_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Synchronous ingest path used by tests and the local-file job.

    For openai mode the caller must use the async variant (ingest_pdf_bytes_async)
    because httpx needs an event loop.  Passing _extracted_override bypasses
    extraction entirely (used by tests).
    """
    pdf_size = len(pdf_bytes)
    if max_pdf_bytes is not None and pdf_size > max_pdf_bytes:
        return {
            "status": "PDF_TOO_LARGE",
            "error": f"PDF size {pdf_size} exceeds max {max_pdf_bytes} bytes",
            "size_bytes": pdf_size,
            "max_pdf_bytes": max_pdf_bytes,
        }

    sha = pdf_sha256(pdf_bytes)
    conn = db()
    try:
        return _ingest_with_conn(
            conn=conn,
            pdf_bytes=pdf_bytes,
            sha=sha,
            mode=mode,
            source_path=source_path,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            openai_model=openai_model,
            reconcile_tolerance=reconcile_tolerance,
            _extracted_override=_extracted_override,
            is_async=False,
        )
    finally:
        conn.close()


async def ingest_pdf_bytes_async(
    pdf_bytes: bytes,
    mode: str,
    source_path: Optional[str] = None,
    openai_api_key: str = "",
    openai_base_url: str = "https://api.openai.com/v1",
    openai_model: str = "gpt-4o-mini",
    reconcile_tolerance: float = 0.02,
    max_pdf_bytes: Optional[int] = None,
) -> Dict[str, Any]:
    """Async ingest path used by the FastAPI endpoints."""
    pdf_size = len(pdf_bytes)
    if max_pdf_bytes is not None and pdf_size > max_pdf_bytes:
        return {
            "status": "PDF_TOO_LARGE",
            "error": f"PDF size {pdf_size} exceeds max {max_pdf_bytes} bytes",
            "size_bytes": pdf_size,
            "max_pdf_bytes": max_pdf_bytes,
        }

    sha = pdf_sha256(pdf_bytes)
    conn = db()
    try:
        return await _ingest_with_conn_async(
            conn=conn,
            pdf_bytes=pdf_bytes,
            sha=sha,
            mode=mode,
            source_path=source_path,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            openai_model=openai_model,
            reconcile_tolerance=reconcile_tolerance,
        )
    finally:
        conn.close()


def _run_extraction_sync(
    mode: str,
    pdf_bytes: bytes,
    openai_api_key: str,
    openai_base_url: str,
    openai_model: str,
    _extracted_override: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if _extracted_override is not None:
        return _extracted_override
    if mode == "stub":
        return _stub_extraction()
    if mode == "text":
        text = extract_pdf_text(pdf_bytes)
        return parse_heritage_pool_invoice(text)
    raise ValueError(f"Use ingest_pdf_bytes_async for mode='{mode}'")


async def _run_extraction_async(
    mode: str,
    pdf_bytes: bytes,
    openai_api_key: str,
    openai_base_url: str,
    openai_model: str,
) -> Dict[str, Any]:
    if mode == "stub":
        return _stub_extraction()
    if mode == "text":
        text = extract_pdf_text(pdf_bytes)
        return parse_heritage_pool_invoice(text)
    if mode == "openai":
        text = extract_pdf_text(pdf_bytes)
        return await extract_via_openai(text, openai_api_key, openai_base_url, openai_model)
    raise ValueError(f"Unknown extraction mode: '{mode}'")


def _ingest_with_conn(
    conn: sqlite3.Connection,
    pdf_bytes: bytes,
    sha: str,
    mode: str,
    source_path: Optional[str],
    openai_api_key: str,
    openai_base_url: str,
    openai_model: str,
    reconcile_tolerance: float,
    _extracted_override: Optional[Dict[str, Any]],
    is_async: bool,
) -> Dict[str, Any]:
    # Duplicate check — PDF hash
    existing = conn.execute(
        "SELECT id, status, vendor_name, invoice_number FROM chemical_invoices WHERE pdf_sha256=?",
        (sha,),
    ).fetchone()
    if existing:
        return {
            "status": "DUPLICATE",
            "invoice_id": existing["id"],
            "sha256": sha,
            "vendor_name": existing["vendor_name"],
            "invoice_number": existing["invoice_number"],
            "message": f"PDF already ingested as invoice #{existing['invoice_number']} (id={existing['id']}, status={existing['status']})",
        }

    started_at = dt.datetime.utcnow().isoformat()

    # Extraction
    try:
        extracted = _run_extraction_sync(mode, pdf_bytes, openai_api_key, openai_base_url, openai_model, _extracted_override)
        extraction_success = True
        extraction_error: Optional[str] = None
    except Exception as exc:
        extracted = None
        extraction_success = False
        extraction_error = str(exc)

    completed_at = dt.datetime.utcnow().isoformat()

    # Record extraction run (invoice_id filled in after insert)
    run_cur = conn.execute(
        """
        INSERT INTO chemical_invoice_extraction_runs
            (invoice_id, pdf_sha256, extraction_mode, started_at, completed_at, success, error_message, raw_output)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None, sha, mode, started_at, completed_at,
            1 if extraction_success else 0,
            extraction_error,
            json.dumps(extracted)[:10000] if extracted else None,
        ),
    )
    run_id = run_cur.lastrowid

    if not extraction_success:
        conn.commit()
        return {
            "status": "EXTRACTION_FAILED",
            "error": extraction_error,
            "message": "Invoice extraction failed; file was not ingested.",
            "extraction_run_id": run_id,
            "sha256": sha,
        }

    return _persist_extracted(
        conn=conn,
        extracted=extracted,
        sha=sha,
        mode=mode,
        source_path=source_path,
        reconcile_tolerance=reconcile_tolerance,
        run_id=run_id,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=openai_model,
    )


async def _ingest_with_conn_async(
    conn: sqlite3.Connection,
    pdf_bytes: bytes,
    sha: str,
    mode: str,
    source_path: Optional[str],
    openai_api_key: str,
    openai_base_url: str,
    openai_model: str,
    reconcile_tolerance: float,
) -> Dict[str, Any]:
    existing = conn.execute(
        "SELECT id, status, vendor_name, invoice_number FROM chemical_invoices WHERE pdf_sha256=?",
        (sha,),
    ).fetchone()
    if existing:
        return {
            "status": "DUPLICATE",
            "invoice_id": existing["id"],
            "sha256": sha,
            "vendor_name": existing["vendor_name"],
            "invoice_number": existing["invoice_number"],
            "message": f"PDF already ingested as invoice #{existing['invoice_number']} (id={existing['id']}, status={existing['status']})",
        }

    started_at = dt.datetime.utcnow().isoformat()

    try:
        extracted = await _run_extraction_async(mode, pdf_bytes, openai_api_key, openai_base_url, openai_model)
        extraction_success = True
        extraction_error = None
    except Exception as exc:
        extracted = None
        extraction_success = False
        extraction_error = str(exc)

    completed_at = dt.datetime.utcnow().isoformat()

    run_cur = conn.execute(
        """
        INSERT INTO chemical_invoice_extraction_runs
            (invoice_id, pdf_sha256, extraction_mode, started_at, completed_at, success, error_message, raw_output)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None, sha, mode, started_at, completed_at,
            1 if extraction_success else 0,
            extraction_error,
            json.dumps(extracted)[:10000] if extracted else None,
        ),
    )
    run_id = run_cur.lastrowid

    if not extraction_success:
        conn.commit()
        return {
            "status": "EXTRACTION_FAILED",
            "error": extraction_error,
            "message": "Invoice extraction failed; file was not ingested.",
            "extraction_run_id": run_id,
            "sha256": sha,
        }

    return _persist_extracted(
        conn=conn,
        extracted=extracted,
        sha=sha,
        mode=mode,
        source_path=source_path,
        reconcile_tolerance=reconcile_tolerance,
        run_id=run_id,
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_model=openai_model,
    )


def _persist_extracted(
    conn: sqlite3.Connection,
    extracted: Dict[str, Any],
    sha: str,
    mode: str,
    source_path: Optional[str],
    reconcile_tolerance: float,
    run_id: Optional[int],
    openai_api_key: str = "",
    openai_base_url: str = "https://api.openai.com/v1",
    openai_model: str = "gpt-4o-mini",
) -> Dict[str, Any]:
    """Normalize, reconcile, and write the extracted invoice to the DB."""

    vendor_name = str(extracted.get("vendor_name") or "Unknown")
    invoice_number = extracted.get("invoice_number")

    # Duplicate check — vendor + invoice_number
    if vendor_name and invoice_number:
        dup = conn.execute(
            "SELECT id FROM chemical_invoices WHERE vendor_name=? AND invoice_number=?",
            (vendor_name, invoice_number),
        ).fetchone()
        if dup:
            conn.commit()
            return {
                "status": "DUPLICATE",
                "invoice_id": dup["id"],
                "sha256": sha,
                "vendor_name": vendor_name,
                "invoice_number": invoice_number,
                "message": f"Invoice {invoice_number} from {vendor_name} already exists (id={dup['id']})",
            }

    # Normalize each line item
    line_items: List[Dict[str, Any]] = list(extracted.get("line_items") or [])
    allow_ai_classification = (
        mode == "openai"
        and _env_flag("AI_GATE_ENABLED", "0")
        and CHEM_INVOICE_AI_CLASSIFICATION_ENABLED
    )
    for item in line_items:
        ai_sugg = item.pop("ai_suggestion", None)
        norm = normalize_line(
            conn,
            vendor_name=vendor_name,
            item_code=item.get("item_code"),
            raw_description=item.get("raw_description", ""),
            ai_suggestion=ai_sugg,
            allow_ai_classification=allow_ai_classification,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            openai_model=openai_model,
        )
        item.update(norm)

    # Reconcile
    rec = reconcile(
        line_items=line_items,
        subtotal=extracted.get("subtotal"),
        tax=extracted.get("tax"),
        fees=extracted.get("fees", 0.0),
        invoice_total=extracted.get("invoice_total"),
        tolerance=reconcile_tolerance,
    )
    invoice_status = rec["status"]
    if not invoice_number and invoice_status == "RECONCILED":
        invoice_status = "REVIEW_NEEDED"
        rec["reconciliation"]["missing_invoice_number"] = True

    now = dt.datetime.utcnow().isoformat()
    inv_cur = conn.execute(
        """
        INSERT INTO chemical_invoices
            (pdf_sha256, vendor_name, branch_location, invoice_number, invoice_date,
             account_number, po_number, ordered_by, invoice_total, subtotal, tax, fees,
             status, reconciliation_json, extraction_mode, ingested_at, pdf_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sha,
            vendor_name,
            extracted.get("branch_location"),
            invoice_number,
            extracted.get("invoice_date"),
            extracted.get("account_number"),
            extracted.get("po_number"),
            extracted.get("ordered_by"),
            extracted.get("invoice_total"),
            extracted.get("subtotal"),
            extracted.get("tax"),
            float(extracted.get("fees") or 0),
            invoice_status,
            json.dumps(rec["reconciliation"]),
            mode,
            now,
            source_path,
        ),
    )
    invoice_id = inv_cur.lastrowid

    # Back-fill extraction run with the new invoice_id
    if run_id is not None:
        conn.execute(
            "UPDATE chemical_invoice_extraction_runs SET invoice_id=? WHERE id=?",
            (invoice_id, run_id),
        )

    for item in line_items:
        conn.execute(
            """
            INSERT INTO chemical_invoice_lines
                (invoice_id, line_number, item_code, raw_description,
                 normalized_product_name, category, cost_type,
                 quantity, unit, default_unit, unit_price, extended_price,
                 confidence, alias_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_id,
                item.get("line_number"),
                item.get("item_code"),
                item.get("raw_description"),
                item.get("normalized_product_name"),
                item.get("category"),
                item.get("cost_type"),
                item.get("quantity"),
                item.get("unit"),
                item.get("default_unit"),
                item.get("unit_price"),
                item.get("extended_price"),
                item.get("confidence"),
                item.get("alias_id"),
            ),
        )

    conn.commit()

    return {
        "status": invoice_status,
        "invoice_id": invoice_id,
        "sha256": sha,
        "invoice_number": invoice_number,
        "vendor_name": vendor_name,
        "invoice_total": extracted.get("invoice_total"),
        "line_count": len(line_items),
        "reconciliation": rec["reconciliation"],
    }


# ─── Local inbox scan ─────────────────────────────────────────────────────────


def scan_inbox(
    inbox_dir: str,
    processed_dir: str,
    failed_dir: str,
    mode: str,
    openai_api_key: str = "",
    openai_base_url: str = "https://api.openai.com/v1",
    openai_model: str = "gpt-4o-mini",
    reconcile_tolerance: float = 0.02,
    max_pdf_bytes: int = 25 * 1024 * 1024,
) -> Dict[str, Any]:
    """
    Scan inbox_dir for PDF files, ingest each one synchronously (stub/text modes).
    Move processed files to processed_dir on success, failed_dir on error.
    Returns a summary dict.

    For openai mode use scan_inbox_async.
    """
    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(failed_dir, exist_ok=True)

    import glob
    import shutil

    pdfs = sorted(glob.glob(os.path.join(inbox_dir, "*.pdf")))
    results: List[Dict[str, Any]] = []

    for pdf_path in pdfs:
        result: Dict[str, Any]
        file_sha = file_sha256(pdf_path)
        try:
            size_bytes = os.path.getsize(pdf_path)
            if size_bytes > max_pdf_bytes:
                result = {
                    "status": "PDF_TOO_LARGE",
                    "error": f"PDF size {size_bytes} exceeds max {max_pdf_bytes} bytes",
                    "size_bytes": size_bytes,
                    "max_pdf_bytes": max_pdf_bytes,
                    "sha256": file_sha,
                }
            else:
                with open(pdf_path, "rb") as fh:
                    pdf_bytes = fh.read()
                result = ingest_pdf_bytes(
                    pdf_bytes=pdf_bytes,
                    mode=mode,
                    source_path=pdf_path,
                    openai_api_key=openai_api_key,
                    openai_base_url=openai_base_url,
                    openai_model=openai_model,
                    reconcile_tolerance=reconcile_tolerance,
                    max_pdf_bytes=max_pdf_bytes,
                )
        except Exception as exc:
            result = {"status": "ERROR", "error": str(exc), "sha256": file_sha}

        safe_name = build_safe_pdf_name(result.get("vendor_name"), result.get("invoice_number"), result.get("sha256") or file_sha)
        result["file"] = safe_name
        results.append(result)

        dest_root = failed_dir if result.get("status") in FAILED_SCAN_STATUSES else processed_dir
        dest_path = unique_dest_path(dest_root, safe_name)
        try:
            shutil.move(pdf_path, dest_path)
        except Exception:
            pass

    counts = {"RECONCILED": 0, "REVIEW_NEEDED": 0, "DUPLICATE": 0, "ERROR": 0, "EXTRACTION_FAILED": 0, "PDF_TOO_LARGE": 0}
    for r in results:
        s = r.get("status", "ERROR")
        counts[s] = counts.get(s, 0) + 1

    return {"scanned": len(pdfs), "counts": counts, "results": results}


async def scan_inbox_async(
    inbox_dir: str,
    processed_dir: str,
    failed_dir: str,
    mode: str,
    openai_api_key: str = "",
    openai_base_url: str = "https://api.openai.com/v1",
    openai_model: str = "gpt-4o-mini",
    reconcile_tolerance: float = 0.02,
    max_pdf_bytes: int = 25 * 1024 * 1024,
) -> Dict[str, Any]:
    """Async variant that supports all extraction modes including openai."""
    import glob
    import shutil

    os.makedirs(processed_dir, exist_ok=True)
    os.makedirs(failed_dir, exist_ok=True)

    pdfs = sorted(glob.glob(os.path.join(inbox_dir, "*.pdf")))
    results: List[Dict[str, Any]] = []

    for pdf_path in pdfs:
        result: Dict[str, Any]
        file_sha = file_sha256(pdf_path)
        try:
            size_bytes = os.path.getsize(pdf_path)
            if size_bytes > max_pdf_bytes:
                result = {
                    "status": "PDF_TOO_LARGE",
                    "error": f"PDF size {size_bytes} exceeds max {max_pdf_bytes} bytes",
                    "size_bytes": size_bytes,
                    "max_pdf_bytes": max_pdf_bytes,
                    "sha256": file_sha,
                }
            else:
                with open(pdf_path, "rb") as fh:
                    pdf_bytes = fh.read()
                result = await ingest_pdf_bytes_async(
                    pdf_bytes=pdf_bytes,
                    mode=mode,
                    source_path=pdf_path,
                    openai_api_key=openai_api_key,
                    openai_base_url=openai_base_url,
                    openai_model=openai_model,
                    reconcile_tolerance=reconcile_tolerance,
                    max_pdf_bytes=max_pdf_bytes,
                )
        except Exception as exc:
            result = {"status": "ERROR", "error": str(exc), "sha256": file_sha}

        safe_name = build_safe_pdf_name(result.get("vendor_name"), result.get("invoice_number"), result.get("sha256") or file_sha)
        result["file"] = safe_name
        results.append(result)

        dest_root = failed_dir if result.get("status") in FAILED_SCAN_STATUSES else processed_dir
        dest_path = unique_dest_path(dest_root, safe_name)
        try:
            shutil.move(pdf_path, dest_path)
        except Exception:
            pass

    counts: Dict[str, int] = {"RECONCILED": 0, "REVIEW_NEEDED": 0, "DUPLICATE": 0, "ERROR": 0, "EXTRACTION_FAILED": 0, "PDF_TOO_LARGE": 0}
    for r in results:
        s = r.get("status", "ERROR")
        counts[s] = counts.get(s, 0) + 1

    return {"scanned": len(pdfs), "counts": counts, "results": results}


# ─── Query helpers (used by API endpoints) ────────────────────────────────────


def get_invoice(invoice_id: int) -> Optional[Dict[str, Any]]:
    conn = db()
    row = conn.execute("SELECT * FROM chemical_invoices WHERE id=?", (invoice_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    inv = dict(row)
    inv["reconciliation"] = json.loads(inv.get("reconciliation_json") or "null")
    lines = conn.execute(
        "SELECT * FROM chemical_invoice_lines WHERE invoice_id=? ORDER BY line_number",
        (invoice_id,),
    ).fetchall()
    inv["line_items"] = [dict(l) for l in lines]
    conn.close()
    return inv


def list_invoices(
    status: Optional[str] = None,
    vendor: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    status = validate_invoice_status(status)
    limit, offset = clamp_limit_offset(limit, offset)
    conn = db()
    where_clauses: List[str] = []
    params: List[Any] = []
    if status:
        where_clauses.append("status=?")
        params.append(status)
    if vendor:
        where_clauses.append("vendor_name LIKE ?")
        params.append(f"%{vendor}%")
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT * FROM chemical_invoices {where} ORDER BY invoice_date DESC, ingested_at DESC LIMIT ? OFFSET ?",  # nosec B608
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def invoice_summary(start_date: Optional[str], end_date: Optional[str]) -> Dict[str, Any]:
    start_date, end_date = validate_summary_dates(start_date, end_date)
    conn = db()

    date_filter = ""
    params: List[Any] = []
    if start_date:
        date_filter += " AND i.invoice_date >= ?"
        params.append(start_date)
    if end_date:
        date_filter += " AND i.invoice_date <= ?"
        params.append(end_date)

    inv_rows = conn.execute(
        f"""
        SELECT status, invoice_total, vendor_name
        FROM chemical_invoices i
        WHERE 1=1 {date_filter}
        """,  # nosec B608
        params,
    ).fetchall()

    total_spend = sum(float(r["invoice_total"] or 0) for r in inv_rows)
    invoice_count = len(inv_rows)
    reconciled_count = sum(1 for r in inv_rows if r["status"] == "RECONCILED")
    review_needed_count = sum(1 for r in inv_rows if r["status"] == "REVIEW_NEEDED")

    by_vendor: Dict[str, Any] = {}
    for r in inv_rows:
        v = r["vendor_name"] or "Unknown"
        if v not in by_vendor:
            by_vendor[v] = {"spend": 0.0, "invoices": 0}
        by_vendor[v]["spend"] = round(by_vendor[v]["spend"] + float(r["invoice_total"] or 0), 2)
        by_vendor[v]["invoices"] += 1

    line_rows = conn.execute(
        f"""
        SELECT l.normalized_product_name, l.category, l.cost_type,
               l.default_unit, l.extended_price, l.quantity
        FROM chemical_invoice_lines l
        JOIN chemical_invoices i ON l.invoice_id = i.id
        WHERE 1=1 {date_filter}
        """,  # nosec B608
        params,
    ).fetchall()

    by_category: Dict[str, Any] = {}
    by_product: Dict[str, Any] = {}
    by_cost_type: Dict[str, Any] = {}
    chemical_spend = 0.0

    for l in line_rows:
        price = float(l["extended_price"] or 0)
        cat = l["category"] or "other"
        prod = l["normalized_product_name"] or "unknown"
        ct = l["cost_type"] or "misc"

        if ct == "chemical":
            chemical_spend = round(chemical_spend + price, 2)

        by_category.setdefault(cat, {"spend": 0.0, "line_count": 0})
        by_category[cat]["spend"] = round(by_category[cat]["spend"] + price, 2)
        by_category[cat]["line_count"] += 1

        by_product.setdefault(prod, {"spend": 0.0, "quantity": 0.0, "default_unit": l["default_unit"]})
        by_product[prod]["spend"] = round(by_product[prod]["spend"] + price, 2)
        by_product[prod]["quantity"] = round(by_product[prod]["quantity"] + float(l["quantity"] or 0), 4)

        by_cost_type.setdefault(ct, {"spend": 0.0})
        by_cost_type[ct]["spend"] = round(by_cost_type[ct]["spend"] + price, 2)

    conn.close()
    return {
        "period": {"start_date": start_date, "end_date": end_date},
        "total_spend": round(total_spend, 2),
        "chemical_spend": chemical_spend,
        "invoice_count": invoice_count,
        "reconciled_count": reconciled_count,
        "review_needed_count": review_needed_count,
        "by_vendor": by_vendor,
        "by_category": by_category,
        "by_product": by_product,
        "by_cost_type": by_cost_type,
    }


def mark_invoice_reviewed(invoice_id: int, reviewed_by: Optional[str] = None) -> bool:
    conn = db()
    now = dt.datetime.utcnow().isoformat()
    cur = conn.execute(
        "UPDATE chemical_invoices SET status='REVIEWED', reviewed_at=?, reviewed_by=? WHERE id=?",
        (now, reviewed_by, invoice_id),
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_aliases(
    status: Optional[str] = None,
    vendor: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    status = validate_alias_status(status)
    limit, offset = clamp_limit_offset(limit, offset)
    conn = db()
    where_clauses: List[str] = []
    params: List[Any] = []
    if status:
        where_clauses.append("mapping_status=?")
        params.append(status)
    if vendor:
        where_clauses.append("vendor_name LIKE ?")
        params.append(f"%{vendor}%")
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    params += [limit, offset]
    rows = conn.execute(
        f"SELECT * FROM chemical_product_aliases {where} ORDER BY times_seen DESC, first_seen_at DESC LIMIT ? OFFSET ?",  # nosec B608
        params,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_alias(
    alias_id: int,
    approved_by: Optional[str] = None,
    updates: Optional[Dict[str, Any]] = None,
) -> bool:
    conn = db()
    now = dt.datetime.utcnow().isoformat()
    row = conn.execute("SELECT * FROM chemical_product_aliases WHERE id=?", (alias_id,)).fetchone()
    if row is None:
        conn.close()
        return False

    fields = {
        "mapping_status": "APPROVED",
        "approved_by": approved_by,
        "approved_at": now,
    }
    if updates:
        fields.update(validate_alias_updates(updates))

    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE chemical_product_aliases SET {set_clause} WHERE id=?",  # nosec B608
        list(fields.values()) + [alias_id],
    )
    conn.commit()
    conn.close()
    return True


def update_alias(alias_id: int, updates: Dict[str, Any]) -> bool:
    conn = db()
    row = conn.execute("SELECT id FROM chemical_product_aliases WHERE id=?", (alias_id,)).fetchone()
    if row is None:
        conn.close()
        return False

    fields = validate_alias_updates(updates)
    if not fields:
        conn.close()
        return False

    set_clause = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE chemical_product_aliases SET {set_clause} WHERE id=?",  # nosec B608
        list(fields.values()) + [alias_id],
    )
    conn.commit()
    conn.close()
    return True
