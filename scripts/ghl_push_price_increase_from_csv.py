#!/usr/bin/env python3
"""
Push finalized price-increase values from a CSV into matching GHL contacts.

Typical use case:
- read the finalized `sri_042026_price_export.csv`
- use `final_new_rate` when present, otherwise fall back to `projected_new_total_rate`
- match each Skimmer customer to an existing GHL contact using the same customer-sync logic
- update the chosen GHL contact custom field (default: `monthly_price`)
- optionally add a GHL tag (default: `sri-042026-send`)

Dry run by default. Use --apply to actually update contacts, and add --apply-tag
if you also want to attach the send tag.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pg import DATABASE_URL, pg  # type: ignore
from services.customer_sync import (  # type: ignore
    PROTECTED_GHL_TYPES,
    build_customer_sync_record,
    build_ghl_contact_indexes,
    clean_text,
    customer_display_name,
    match_ghl_contact,
)


GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")
GHL_TOKEN = os.getenv("GHL_TOKEN", "")
GHL_VERSION = os.getenv("GHL_VERSION", "2021-07-28")
SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "")
DEFAULT_FIELD_KEY = "monthly_price"
DEFAULT_TAG = "sri-042026-send"


def default_csv_path() -> str:
    env_value = clean_text(os.getenv("PRICE_EXPORT_CSV_PATH"))
    if env_value:
        return env_value
    repo_candidate = ROOT / "sri_042026_price_export.csv"
    if repo_candidate.is_file():
        return str(repo_candidate)
    return "/data/skimmer/sri_042026_price_export.csv"


def build_parser() -> argparse.ArgumentParser:
    csv_default = default_csv_path()
    parser = argparse.ArgumentParser(
        description="Push finalized price-increase values from CSV into GHL contacts and add a send tag."
    )
    parser.add_argument("--csv", default=csv_default, help=f"Path to pricing CSV. Default: {csv_default}")
    parser.add_argument("--field-key", default=DEFAULT_FIELD_KEY, help=f"GHL contact custom field key/name. Default: {DEFAULT_FIELD_KEY}")
    parser.add_argument("--tag", default=DEFAULT_TAG, help=f"GHL tag to add. Default: {DEFAULT_TAG}")
    parser.add_argument("--limit", type=int, default=0, help="Limit processed customers after CSV normalization.")
    parser.add_argument("--include-inactive", action="store_true", help="Allow inactive Skimmer customers from the CSV source.")
    parser.add_argument("--include-leads", action="store_true", help="Allow lead Skimmer customers from the CSV source.")
    parser.add_argument("--create-tag-if-missing", action="store_true", help="Create the GHL tag if it does not already exist.")
    parser.add_argument("--apply", action="store_true", help="Actually update the GHL field and add the tag.")
    parser.add_argument("--apply-tag", action="store_true", help="When used with --apply, also attach the configured GHL tag.")
    return parser


def _ghl_headers() -> Dict[str, str]:
    if not GHL_TOKEN:
        raise RuntimeError("GHL_TOKEN is not set")
    if not GHL_LOCATION_ID:
        raise RuntimeError("GHL_LOCATION_ID is not set")
    return {
        "Authorization": f"Bearer {GHL_TOKEN}",
        "Version": GHL_VERSION,
        "LocationId": GHL_LOCATION_ID,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def ghl_post(client: httpx.Client, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = client.post(GHL_BASE_URL.rstrip("/") + path, headers=_ghl_headers(), json=payload)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def ghl_get(client: httpx.Client, path: str) -> Dict[str, Any]:
    response = client.get(GHL_BASE_URL.rstrip("/") + path, headers=_ghl_headers())
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def ghl_put(client: httpx.Client, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = client.put(GHL_BASE_URL.rstrip("/") + path, headers=_ghl_headers(), json=payload)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def ghl_fetch_all_contacts(client: httpx.Client, page_limit: int = 100) -> List[Dict[str, Any]]:
    contacts: List[Dict[str, Any]] = []
    search_after: Optional[List[Any]] = None

    while True:
        payload: Dict[str, Any] = {
            "locationId": GHL_LOCATION_ID,
            "pageLimit": page_limit,
        }
        if search_after:
            payload["searchAfter"] = search_after

        data = ghl_post(client, "/contacts/search", payload)
        page = data.get("contacts")
        if not isinstance(page, list) or not page:
            break

        contacts.extend([contact for contact in page if isinstance(contact, dict)])
        last = page[-1] if isinstance(page[-1], dict) else {}
        search_after = last.get("searchAfter")
        if len(page) < page_limit or not search_after:
            break

    return contacts


def ghl_get_location_tags(client: httpx.Client) -> List[Dict[str, Any]]:
    data = ghl_get(client, f"/locations/{GHL_LOCATION_ID}/tags")
    for key in ("tags", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def ghl_find_tag_by_name(client: httpx.Client, tag_name: str) -> Optional[Dict[str, Any]]:
    target = clean_text(tag_name).lower()
    for tag in ghl_get_location_tags(client):
        name = clean_text(tag.get("name") or tag.get("label"))
        if name.lower() == target:
            return tag
    return None


def ghl_create_tag(client: httpx.Client, tag_name: str) -> Dict[str, Any]:
    return ghl_post(client, f"/locations/{GHL_LOCATION_ID}/tags", {"name": tag_name})


def ghl_add_tags_to_contact(client: httpx.Client, contact_id: str, tags: Sequence[str]) -> Dict[str, Any]:
    return ghl_post(client, f"/contacts/{contact_id}/tags", {"tags": list(tags)})


def ghl_get_custom_fields(client: httpx.Client) -> List[Dict[str, Any]]:
    data = ghl_get(client, f"/locations/{GHL_LOCATION_ID}/customFields")
    for key in ("customFields", "fields", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def ghl_find_custom_field(client: httpx.Client, field_key: str) -> Optional[Dict[str, Any]]:
    target = clean_text(field_key).lower()
    for field in ghl_get_custom_fields(client):
        candidates = [
            clean_text(field.get("id")),
            clean_text(field.get("name")),
            clean_text(field.get("fieldKey")),
            clean_text(field.get("key")),
            clean_text(field.get("slug")),
        ]
        if any(value.lower() == target for value in candidates if value):
            return field
    return None


def _skimmer_headers() -> Dict[str, str]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")
    return {"skimmer-api-key": SKIMMER_API_KEY, "Accept": "application/json"}


def skimmer_get(client: httpx.Client, path: str) -> Any:
    response = client.get(SKIMMER_API_BASE_URL.rstrip("/") + path, headers=_skimmer_headers())
    response.raise_for_status()
    return response.json()


def skimmer_fetch_all_customers(client: httpx.Client) -> List[Dict[str, Any]]:
    data = skimmer_get(client, "/Customers")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def load_ghl_contact_hints() -> Dict[str, str]:
    if not DATABASE_URL:
        return {}
    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_customer_id, ghl_contact_id
                FROM sk_customer
                WHERE source_system = 'skimmer'
                  AND ghl_contact_id IS NOT NULL
                  AND ghl_contact_id <> ''
                """
            )
            rows = cur.fetchall()
    return {
        clean_text(row["source_customer_id"]): clean_text(row["ghl_contact_id"])
        for row in rows
        if clean_text(row.get("source_customer_id")) and clean_text(row.get("ghl_contact_id"))
    }


def normalize_money(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"Invalid money value: {value!r}")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    normalized = format(amount, "f")
    if normalized.endswith(".00"):
        return normalized[:-3]
    if normalized.endswith("0"):
        return normalized[:-1]
    return normalized


def choose_rate(row: Dict[str, str]) -> str:
    for key in ("final_new_rate", "projected_new_total_rate"):
        value = normalize_money(row.get(key))
        if value:
            return value
    return ""


def load_targets_from_csv(csv_path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    grouped: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        customer_id = clean_text(row.get("customer_id"))
        if not customer_id or customer_id.upper() == "TOTAL":
            continue
        grouped[customer_id].append(row)

    candidates: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for customer_id, items in grouped.items():
        rates = sorted({choose_rate(item) for item in items if choose_rate(item)})
        if not rates:
            conflicts.append(
                {
                    "customer_id": customer_id,
                    "customer": clean_text(items[0].get("full_name")),
                    "action": "skip_missing_rate",
                }
            )
            continue
        if len(rates) > 1:
            conflicts.append(
                {
                    "customer_id": customer_id,
                    "customer": clean_text(items[0].get("full_name")),
                    "action": "skip_conflicting_rates",
                    "rates": rates,
                    "service_location_ids": [clean_text(item.get("service_location_id")) for item in items],
                }
            )
            continue

        row = items[0]
        candidates.append(
            {
                "customer_id": customer_id,
                "display_name": clean_text(row.get("full_name")),
                "monthly_price": rates[0],
                "source_rows": len(items),
            }
        )

    candidates.sort(key=lambda item: (item["display_name"].lower(), item["customer_id"]))
    conflicts.sort(key=lambda item: (clean_text(item.get("customer")).lower(), clean_text(item.get("customer_id"))))
    return candidates, conflicts


def build_skimmer_customer_map(client: httpx.Client) -> Dict[str, Dict[str, Any]]:
    results: Dict[str, Dict[str, Any]] = {}
    for customer in skimmer_fetch_all_customers(client):
        customer_id = clean_text(customer.get("id"))
        if not customer_id:
            continue
        first_name = clean_text(customer.get("firstName"))
        last_name = clean_text(customer.get("lastName"))
        company_name = clean_text(customer.get("companyName"))
        results[customer_id] = {
            "customer_id": customer_id,
            "first_name": first_name,
            "last_name": last_name,
            "company_name": company_name,
            "display_name": customer_display_name(
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "company_name": company_name,
                    "source_customer_id": customer_id,
                }
            ),
            "primary_email": clean_text(customer.get("primaryEmail")),
            "mobile_phone": clean_text(customer.get("mobilePhone")),
            "is_inactive": bool(customer.get("isInactive")),
            "is_lead": bool(customer.get("isLead")),
        }
    return results


def build_sync_record(candidate: Dict[str, Any], skimmer_customer: Dict[str, Any], ghl_contact_hint: str) -> Dict[str, Any]:
    row = {
        "id": candidate.get("customer_id"),
        "source_customer_id": candidate.get("customer_id"),
        "first_name": skimmer_customer.get("first_name"),
        "last_name": skimmer_customer.get("last_name"),
        "company_name": skimmer_customer.get("company_name"),
        "email": skimmer_customer.get("primary_email"),
        "phone": skimmer_customer.get("mobile_phone"),
        "mobile_phone": skimmer_customer.get("mobile_phone"),
        "is_inactive": skimmer_customer.get("is_inactive"),
        "is_lead": skimmer_customer.get("is_lead"),
        "ghl_contact_id": ghl_contact_hint,
    }
    return build_customer_sync_record(row)


def extract_contact_custom_field_value(contact: Dict[str, Any], field_id: str) -> str:
    custom_fields = contact.get("customFields")
    if not isinstance(custom_fields, list):
        return ""
    target = clean_text(field_id)
    for item in custom_fields:
        if not isinstance(item, dict):
            continue
        if clean_text(item.get("id")) != target:
            continue
        value = item.get("value")
        if value is None:
            return ""
        if isinstance(value, (str, int, float)):
            return clean_text(value)
        return clean_text(json.dumps(value, sort_keys=True))
    return ""


def contact_has_tag(contact: Dict[str, Any], tag_name: str) -> bool:
    existing = contact.get("tags")
    names: List[str] = []
    if isinstance(existing, list):
        for item in existing:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("label")
                if name:
                    names.append(str(name))
    elif isinstance(existing, str):
        names.append(existing)
    tag_name_lc = clean_text(tag_name).lower()
    return any(clean_text(name).lower() == tag_name_lc for name in names)


def build_contact_custom_field_payload(field_id: str, value: str) -> Dict[str, Any]:
    return {"customFields": [{"id": field_id, "value": value}]}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    csv_candidates, csv_conflicts = load_targets_from_csv(args.csv)
    if args.limit and args.limit > 0:
        csv_candidates = csv_candidates[: args.limit]

    ghl_hints = load_ghl_contact_hints()

    with httpx.Client(timeout=30.0) as client:
        field = ghl_find_custom_field(client, args.field_key)
        if not field:
            raise RuntimeError(f"GHL custom field not found for key/name/id: {args.field_key}")
        field_id = clean_text(field.get("id"))

        tag = ghl_find_tag_by_name(client, args.tag)
        if not tag and args.create_tag_if_missing and args.apply_tag:
            tag = ghl_create_tag(client, args.tag)
        tag_exists = tag is not None

        contacts = ghl_fetch_all_contacts(client)
        indexes = build_ghl_contact_indexes(contacts)
        skimmer_by_id = build_skimmer_customer_map(client)

        summary: Dict[str, Any] = {
            "dry_run": not args.apply,
            "apply_tag": bool(args.apply and args.apply_tag),
            "csv_path": args.csv,
            "field_key_requested": args.field_key,
            "field_id": field_id,
            "field_name": clean_text(field.get("name") or field.get("fieldKey") or field.get("key")),
            "tag": args.tag,
            "tag_exists": tag_exists,
            "csv_candidates": len(csv_candidates),
            "csv_conflicts": len(csv_conflicts),
            "ghl_contacts_loaded": len(contacts),
            "matched": 0,
            "updated_field": 0,
            "field_already_set": 0,
            "tagged": 0,
            "already_tagged": 0,
            "skipped_inactive": 0,
            "skipped_lead": 0,
            "skipped_missing_skimmer_customer": 0,
            "skipped_protected": 0,
            "skipped_ambiguous": 0,
            "not_found": 0,
            "errors": 0,
            "items": list(csv_conflicts),
        }

        for candidate in csv_candidates:
            customer_id = clean_text(candidate.get("customer_id"))
            skimmer_customer = skimmer_by_id.get(customer_id)
            if not skimmer_customer:
                summary["skipped_missing_skimmer_customer"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": candidate.get("display_name"),
                        "action": "skip_missing_skimmer_customer",
                    }
                )
                continue

            if skimmer_customer.get("is_inactive") and not args.include_inactive:
                summary["skipped_inactive"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": skimmer_customer.get("display_name"),
                        "action": "skip_inactive",
                    }
                )
                continue

            if skimmer_customer.get("is_lead") and not args.include_leads:
                summary["skipped_lead"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": skimmer_customer.get("display_name"),
                        "action": "skip_lead",
                    }
                )
                continue

            record = build_sync_record(candidate, skimmer_customer, ghl_hints.get(customer_id, ""))
            matched_contact, methods, ambiguous = match_ghl_contact(record, indexes)

            if ambiguous:
                summary["skipped_ambiguous"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": skimmer_customer.get("display_name"),
                        "action": "skip_ambiguous",
                        "match_methods": methods,
                    }
                )
                continue

            if matched_contact is None:
                summary["not_found"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": skimmer_customer.get("display_name"),
                        "action": "not_found",
                    }
                )
                continue

            summary["matched"] += 1
            contact_id = clean_text(matched_contact.get("id"))
            current_type = clean_text(matched_contact.get("type")).lower()
            if current_type in PROTECTED_GHL_TYPES:
                summary["skipped_protected"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": skimmer_customer.get("display_name"),
                        "action": "skip_protected_type",
                        "ghl_contact_id": contact_id,
                        "ghl_type": current_type,
                    }
                )
                continue

            desired_value = clean_text(candidate.get("monthly_price"))
            existing_value = extract_contact_custom_field_value(matched_contact, field_id)
            needs_field_update = normalize_money(existing_value) != normalize_money(desired_value)
            already_tagged = contact_has_tag(matched_contact, args.tag)

            item: Dict[str, Any] = {
                "customer_id": customer_id,
                "customer": skimmer_customer.get("display_name"),
                "ghl_contact_id": contact_id,
                "match_methods": methods,
                "monthly_price": desired_value,
                "existing_monthly_price": existing_value,
                "field_update_needed": needs_field_update,
                "tag_already_present": already_tagged,
            }

            if not args.apply:
                item["action"] = "would_update_and_tag" if args.apply_tag else "would_update_field_only"
                summary["items"].append(item)
                continue

            try:
                if needs_field_update:
                    ghl_put(client, f"/contacts/{contact_id}", build_contact_custom_field_payload(field_id, desired_value))
                    summary["updated_field"] += 1
                else:
                    summary["field_already_set"] += 1

                if args.apply_tag:
                    if not tag_exists:
                        raise RuntimeError(f"GHL tag not found: {args.tag}")
                    if already_tagged:
                        summary["already_tagged"] += 1
                    else:
                        ghl_add_tags_to_contact(client, contact_id, [args.tag])
                        summary["tagged"] += 1
                else:
                    item["tag_already_present"] = already_tagged

                item["action"] = "updated_and_tagged" if args.apply_tag else "updated_field_only"
                summary["items"].append(item)
            except Exception as exc:
                summary["errors"] += 1
                item["action"] = "error"
                item["error"] = str(exc)
                summary["items"].append(item)

    print(json.dumps(summary, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
