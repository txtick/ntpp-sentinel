#!/usr/bin/env python3
"""
Remove one Skimmer tag from customers that also carry another Skimmer tag.

Current pricing use case:
- remove `sri-042026`
- from any customer that also has `not-invoiced`
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict, List

import httpx  # type: ignore


SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "")

CUSTOMER_UPDATE_FIELDS = [
    "id",
    "billingAddress",
    "billingCity",
    "billingState",
    "billingZip",
    "tags",
    "firstName",
    "lastName",
    "companyName",
    "mobilePhone",
    "mobileLabel1",
    "mobilePhone2",
    "mobileLabel2",
    "homePhone",
    "workPhone",
    "primaryEmail",
    "secondaryEmail",
    "email3",
    "email4",
    "notes",
    "customerCode",
    "displayAsCompany",
    "primaryEmailIsBilling",
    "secondaryEmailIsBilling",
    "email3IsBilling",
    "email4IsBilling",
    "mobilePhoneSendServiceTexts",
    "leadSourceId",
]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _headers() -> Dict[str, str]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")
    return {
        "skimmer-api-key": SKIMMER_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _extract_tag_objects(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [dict(value)]
    return []


def _extract_tag_names(value: Any) -> List[str]:
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


def _build_customer_update_payload(record: Dict[str, Any], tags: List[Dict[str, Any]]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for field in CUSTOMER_UPDATE_FIELDS:
        if field == "tags":
            payload[field] = tags
        else:
            payload[field] = record.get(field)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove a target Skimmer tag from customers that also have a filter tag."
    )
    parser.add_argument("--remove-tag", required=True, help="Tag to remove, for example sri-042026")
    parser.add_argument("--if-has-tag", required=True, help="Only remove when this other tag is present, for example not-invoiced")
    parser.add_argument("--limit", type=int, default=0, help="Limit matched customers processed.")
    parser.add_argument("--apply", action="store_true", help="Actually apply the removals.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    remove_tag_lc = args.remove_tag.strip().lower()
    filter_tag_lc = args.if_has_tag.strip().lower()

    summary: Dict[str, Any] = {
        "dry_run": not args.apply,
        "remove_tag": args.remove_tag,
        "if_has_tag": args.if_has_tag,
        "matched": 0,
        "removed": 0,
        "would_remove": 0,
        "errors": 0,
        "items": [],
    }

    with httpx.Client(timeout=30.0) as client:
        response = client.get(SKIMMER_API_BASE_URL.rstrip("/") + "/Customers", headers=_headers())
        response.raise_for_status()
        data = response.json()
        customers = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

        for customer in customers:
            tag_names = _extract_tag_names(customer.get("tags"))
            tag_name_set = {tag.lower() for tag in tag_names}
            if remove_tag_lc not in tag_name_set or filter_tag_lc not in tag_name_set:
                continue

            summary["matched"] += 1
            customer_id = clean_text(customer.get("id"))
            display_name = " ".join(
                part for part in [clean_text(customer.get("firstName")), clean_text(customer.get("lastName"))] if part
            ).strip() or clean_text(customer.get("companyName")) or customer_id

            existing_tag_objects = _extract_tag_objects(customer.get("tags"))
            if existing_tag_objects:
                new_tags = [
                    tag
                    for tag in existing_tag_objects
                    if clean_text(tag.get("name") or tag.get("Name")).lower() != remove_tag_lc
                ]
            else:
                new_tags = [{"name": name} for name in tag_names if name.lower() != remove_tag_lc]

            if not args.apply:
                summary["would_remove"] += 1
                summary["items"].append(
                    {
                        "customer_id": customer_id,
                        "customer": display_name,
                        "action": "would_remove",
                    }
                )
            else:
                try:
                    payload = _build_customer_update_payload(customer, new_tags)
                    update = client.put(SKIMMER_API_BASE_URL.rstrip("/") + "/Customers", headers=_headers(), json=payload)
                    update.raise_for_status()
                    summary["removed"] += 1
                    summary["items"].append(
                        {
                            "customer_id": customer_id,
                            "customer": display_name,
                            "action": "removed",
                        }
                    )
                except Exception as exc:
                    summary["errors"] += 1
                    summary["items"].append(
                        {
                            "customer_id": customer_id,
                            "customer": display_name,
                            "action": "error_remove",
                            "error": str(exc),
                        }
                    )

            if args.limit and summary["matched"] >= args.limit:
                break

    print(json.dumps(summary, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
