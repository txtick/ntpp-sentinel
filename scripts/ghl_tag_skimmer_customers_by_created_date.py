#!/usr/bin/env python3
"""
Tag the GHL contacts that correspond to a Skimmer customer cohort selected by
Skimmer customer CreatedAt.

Typical use case:
- customers created before 2025-08-01 should receive a pricing-change tag
- match them to existing GHL contacts using the same logic as skimmer_customer_sync
- optionally create the GHL tag if missing
- optionally apply the tag to the matched contacts
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import httpx  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from pg import DATABASE_URL, pg  # type: ignore
from services.customer_sync import (  # type: ignore
    PROTECTED_GHL_TYPES,
    build_customer_sync_record,
    build_ghl_contact_indexes,
    clean_text,
    contact_needs_update,
    customer_display_name,
    match_ghl_contact,
)
from skimmer_tag_customers_by_created_date import load_candidates  # type: ignore


GHL_BASE_URL = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "")
GHL_TOKEN = os.getenv("GHL_TOKEN", "")
GHL_VERSION = os.getenv("GHL_VERSION", "2021-07-28")
SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "")


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
    tag_name_lc = tag_name.strip().lower()
    for tag in ghl_get_location_tags(client):
        name = clean_text(tag.get("name") or tag.get("label"))
        if name.lower() == tag_name_lc:
            return tag
    return None


def ghl_create_tag(client: httpx.Client, tag_name: str) -> Dict[str, Any]:
    # HighLevel location tag create endpoint
    return ghl_post(client, f"/locations/{GHL_LOCATION_ID}/tags", {"name": tag_name})


def ghl_add_tags_to_contact(client: httpx.Client, contact_id: str, tags: Sequence[str]) -> Dict[str, Any]:
    return ghl_post(client, f"/contacts/{contact_id}/tags", {"tags": list(tags)})


def _skimmer_headers() -> Dict[str, str]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")
    return {"skimmer-api-key": SKIMMER_API_KEY, "Accept": "application/json"}


def skimmer_get(client: httpx.Client, path: str) -> Any:
    response = client.get(SKIMMER_API_BASE_URL.rstrip("/") + path, headers=_skimmer_headers())
    response.raise_for_status()
    return response.json()


def _extract_name_tags(value: Any) -> List[str]:
    names: List[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                name = item.get("name") or item.get("Name")
                if name:
                    names.append(str(name))
    elif isinstance(value, str):
        names.append(value)
    return [clean_text(name) for name in names if clean_text(name)]


def skimmer_fetch_all_customers(client: httpx.Client) -> List[Dict[str, Any]]:
    data = skimmer_get(client, "/Customers")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def load_candidates_from_skimmer_tag(
    client: httpx.Client,
    *,
    skimmer_tag: str,
    include_inactive: bool,
    include_leads: bool,
    company_id: Optional[str],
) -> List[Dict[str, Any]]:
    target_tag_lc = clean_text(skimmer_tag).lower()
    results: List[Dict[str, Any]] = []

    for customer in skimmer_fetch_all_customers(client):
        existing_tags = _extract_name_tags(customer.get("tags"))
        if target_tag_lc not in {tag.lower() for tag in existing_tags}:
            continue

        is_inactive = bool(customer.get("isInactive"))
        is_lead = bool(customer.get("isLead"))
        if not include_inactive and is_inactive:
            continue
        if not include_leads and is_lead:
            continue

        row_company_id = clean_text(customer.get("companyId"))
        if company_id and row_company_id != company_id:
            continue

        first_name = clean_text(customer.get("firstName"))
        last_name = clean_text(customer.get("lastName"))
        company_name = clean_text(customer.get("companyName"))
        display_name = customer_display_name(
            {
                "first_name": first_name,
                "last_name": last_name,
                "company_name": company_name,
                "source_customer_id": clean_text(customer.get("id")),
            }
        )

        results.append(
            {
                "customer_id": clean_text(customer.get("id")),
                "created_at": clean_text(customer.get("createdAt")),
                "created_at_date": clean_text(customer.get("createdAt"))[:10],
                "first_name": first_name,
                "last_name": last_name,
                "company_name": company_name,
                "display_name": display_name,
                "primary_email": clean_text(customer.get("primaryEmail")),
                "mobile_phone": clean_text(customer.get("mobilePhone")),
                "is_inactive": is_inactive,
                "is_lead": is_lead,
                "company_id": row_company_id,
                "existing_tags": existing_tags,
            }
        )

    results.sort(key=lambda item: (item.get("created_at") or "", item.get("display_name") or "", item.get("customer_id") or ""))
    return results


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


def _candidate_value(candidate: Any, key: str) -> Any:
    if isinstance(candidate, dict):
        return candidate.get(key)
    return getattr(candidate, key, None)


def build_sync_record(candidate: Any, ghl_contact_hint: str) -> Dict[str, Any]:
    row = {
        "id": _candidate_value(candidate, "customer_id"),
        "source_customer_id": _candidate_value(candidate, "customer_id"),
        "first_name": _candidate_value(candidate, "first_name"),
        "last_name": _candidate_value(candidate, "last_name"),
        "company_name": _candidate_value(candidate, "company_name"),
        "email": _candidate_value(candidate, "primary_email"),
        "phone": _candidate_value(candidate, "mobile_phone"),
        "mobile_phone": _candidate_value(candidate, "mobile_phone"),
        "is_inactive": _candidate_value(candidate, "is_inactive"),
        "is_lead": _candidate_value(candidate, "is_lead"),
        "ghl_contact_id": ghl_contact_hint,
    }
    return build_customer_sync_record(row)


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
    tag_name_lc = tag_name.strip().lower()
    return any(clean_text(name).lower() == tag_name_lc for name in names)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tag matching GHL contacts for a Skimmer customer cohort selected by CreatedAt or live Skimmer tag."
    )
    parser.add_argument("--sqlite", help="Path to the Skimmer SQLite export. Defaults to SKIMMER_DB_PATH.")
    parser.add_argument("--cutoff-date", help="Reference YYYY-MM-DD date.")
    parser.add_argument("--mode", choices=["before", "since"], default="before")
    parser.add_argument(
        "--skimmer-tag",
        help="Use live Skimmer API customers currently carrying this Skimmer tag as the source cohort.",
    )
    parser.add_argument("--tag", required=True, help="GHL tag to add.")
    parser.add_argument("--company-id", help="Optional Skimmer CompanyId filter.")
    parser.add_argument("--include-inactive", action="store_true", help="Include inactive customers.")
    parser.add_argument("--include-leads", action="store_true", help="Include leads.")
    parser.add_argument("--limit", type=int, default=0, help="Limit matched customers processed.")
    parser.add_argument("--create-tag-if-missing", action="store_true", help="Create the GHL tag if it does not already exist.")
    parser.add_argument("--apply", action="store_true", help="Actually add the tag in GHL.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    ghl_hints = load_ghl_contact_hints()

    with httpx.Client(timeout=30.0) as client:
        if args.skimmer_tag:
            candidates = load_candidates_from_skimmer_tag(
                client,
                skimmer_tag=args.skimmer_tag,
                include_inactive=args.include_inactive,
                include_leads=args.include_leads,
                company_id=args.company_id,
            )
            source_description = f"live_skimmer_tag:{args.skimmer_tag}"
        else:
            if not args.cutoff_date:
                parser.error("--cutoff-date is required unless --skimmer-tag is used")
            candidates = load_candidates(
                args.sqlite,
                cutoff_date=date.fromisoformat(args.cutoff_date),
                mode=args.mode,
                tag_name=args.tag,
                include_inactive=args.include_inactive,
                include_leads=args.include_leads,
                company_id=args.company_id,
            )
            source_description = f"created_at:{args.mode}:{args.cutoff_date}"

        if args.limit and args.limit > 0:
            candidates = candidates[: args.limit]

        contacts = ghl_fetch_all_contacts(client)
        indexes = build_ghl_contact_indexes(contacts)

        tag = ghl_find_tag_by_name(client, args.tag)
        if not tag and args.create_tag_if_missing:
            tag = ghl_create_tag(client, args.tag)
        tag_exists = tag is not None

        summary: Dict[str, Any] = {
            "dry_run": not args.apply,
            "source": source_description,
            "skimmer_candidates": len(candidates),
            "ghl_contacts_loaded": len(contacts),
            "tag": args.tag,
            "tag_exists": tag_exists,
            "matched": 0,
            "already_tagged": 0,
            "tagged": 0,
            "would_tag": 0,
            "skipped_protected": 0,
            "skipped_ambiguous": 0,
            "not_found": 0,
            "errors": 0,
            "items": [],
        }

        for candidate in candidates:
            candidate_id = clean_text(_candidate_value(candidate, "customer_id"))
            record = build_sync_record(candidate, ghl_hints.get(candidate_id, ""))
            matched_contact, methods, ambiguous = match_ghl_contact(record, indexes)

            if ambiguous:
                summary["skipped_ambiguous"] += 1
                summary["items"].append(
                    {
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
                        "action": "skip_ambiguous",
                        "match_methods": methods,
                    }
                )
                continue

            if matched_contact is None:
                summary["not_found"] += 1
                summary["items"].append(
                    {
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
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
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
                        "action": "skip_protected_type",
                        "ghl_contact_id": contact_id,
                        "ghl_type": current_type,
                    }
                )
                continue

            if contact_has_tag(matched_contact, args.tag):
                summary["already_tagged"] += 1
                summary["items"].append(
                    {
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
                        "action": "already_tagged",
                        "ghl_contact_id": contact_id,
                        "match_methods": methods,
                    }
                )
                continue

            if not args.apply:
                summary["would_tag"] += 1
                summary["items"].append(
                    {
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
                        "action": "would_tag",
                        "ghl_contact_id": contact_id,
                        "match_methods": methods,
                    }
                )
                continue

            try:
                ghl_add_tags_to_contact(client, contact_id, [args.tag])
                summary["tagged"] += 1
                summary["items"].append(
                    {
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
                        "action": "tagged",
                        "ghl_contact_id": contact_id,
                        "match_methods": methods,
                    }
                )
            except Exception as exc:
                summary["errors"] += 1
                summary["items"].append(
                    {
                        "customer_id": candidate_id,
                        "customer": _candidate_value(candidate, "display_name"),
                        "action": "error_tagging",
                        "ghl_contact_id": contact_id,
                        "error": str(exc),
                    }
                )

    print(json.dumps(summary, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
