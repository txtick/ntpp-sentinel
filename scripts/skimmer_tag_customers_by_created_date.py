#!/usr/bin/env python3
"""
Tag Skimmer customers created before a cutoff date.

This script reads directly from a Skimmer SQLite export so we can use the
source `Customer.CreatedAt` field even though Skimmer's UI does not expose it
cleanly for bulk review.

Default behavior is dry-run only. It prints a summary, can export the target
set to CSV/JSON, and can optionally attempt a best-effort API update for each
matched customer.

Why the API mode is configurable:
- this repository already has confirmed Skimmer API GET usage
- it does not yet have a confirmed customer-update endpoint contract
- so the apply step is opt-in and endpoint/method configurable
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_SQLITE_CANDIDATES = [
    os.getenv("SKIMMER_DB_PATH", ""),
    "3f19c6b0c1ef4a1d876f942348997106.db",
]

DEFAULT_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
DEFAULT_API_KEY = os.getenv("SKIMMER_API_KEY", "")
DEFAULT_UPDATE_PATH_TEMPLATE = os.getenv("SKIMMER_CUSTOMER_UPDATE_PATH_TEMPLATE", "/Customers")
DEFAULT_UPDATE_METHOD = os.getenv("SKIMMER_CUSTOMER_UPDATE_METHOD", "PUT").upper()

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


@dataclass
class CandidateCustomer:
    customer_id: str
    created_at: str
    created_at_iso: str
    created_at_date: str
    first_name: str
    last_name: str
    company_name: str
    display_name: str
    primary_email: str
    mobile_phone: str
    is_inactive: bool
    is_lead: bool
    company_id: str
    existing_tags: List[str]


def _resolve_sqlite_path(explicit_path: Optional[str]) -> str:
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_file():
            raise FileNotFoundError(f"SQLite file not found: {path}")
        return str(path)

    for candidate in DEFAULT_SQLITE_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate

    raise FileNotFoundError(
        "Could not find a Skimmer SQLite export. Pass --sqlite explicitly or set SKIMMER_DB_PATH."
    )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_skimmer_timestamp(value: str) -> datetime:
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty timestamp")
    normalized = raw.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def _cutoff_datetime(cutoff_date: date) -> datetime:
    return datetime.combine(cutoff_date, time.min, tzinfo=timezone.utc)


def _existing_tag_map(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    rows = conn.execute(
        """
        SELECT
            ct.CustomerId,
            t.Name
        FROM CustomerTag ct
        JOIN Tag t
          ON t.id = ct.TagId
        WHERE COALESCE(ct.Deleted, 0) = 0
          AND COALESCE(t.Deleted, 0) = 0
          AND t.Name IS NOT NULL
          AND trim(t.Name) <> ''
        """
    ).fetchall()

    result: Dict[str, List[str]] = {}
    for customer_id, tag_name in rows:
        result.setdefault(customer_id, []).append(str(tag_name))
    return result


def _display_name(first_name: str, last_name: str, company_name: str, customer_id: str) -> str:
    person = " ".join(part for part in [first_name.strip(), last_name.strip()] if part).strip()
    if person:
        return person
    if company_name.strip():
        return company_name.strip()
    return customer_id


def load_candidates(
    sqlite_path: str,
    *,
    cutoff_date: date,
    mode: str,
    tag_name: str,
    include_inactive: bool,
    include_leads: bool,
    company_id: Optional[str],
) -> List[CandidateCustomer]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        tag_map = _existing_tag_map(conn)
        rows = conn.execute(
            """
            SELECT
                id,
                CreatedAt,
                FirstName,
                LastName,
                CompanyName,
                PrimaryEmail,
                MobilePhone,
                IsInactive,
                IsLead,
                CompanyId
            FROM Customer
            WHERE COALESCE(Deleted, 0) = 0
            """
        ).fetchall()
    finally:
        conn.close()

    cutoff_dt = _cutoff_datetime(cutoff_date)
    tag_name_lc = tag_name.strip().lower()
    results: List[CandidateCustomer] = []

    for row in rows:
        created_at_raw = str(row["CreatedAt"] or "").strip()
        if not created_at_raw:
            continue

        try:
            created_at_dt = _parse_skimmer_timestamp(created_at_raw)
        except ValueError:
            continue

        if mode == "before":
            if created_at_dt >= cutoff_dt:
                continue
        else:
            if created_at_dt < cutoff_dt:
                continue

        row_company_id = str(row["CompanyId"] or "").strip()
        if company_id and row_company_id != company_id:
            continue

        is_inactive = _parse_bool(row["IsInactive"])
        is_lead = _parse_bool(row["IsLead"])
        if not include_inactive and is_inactive:
            continue
        if not include_leads and is_lead:
            continue

        customer_id = str(row["id"])
        existing_tags = sorted(set(tag_map.get(customer_id, [])))
        existing_tags_lc = {t.strip().lower() for t in existing_tags}
        if tag_name_lc in existing_tags_lc:
            continue

        first_name = str(row["FirstName"] or "")
        last_name = str(row["LastName"] or "")
        company_name = str(row["CompanyName"] or "")

        results.append(
            CandidateCustomer(
                customer_id=customer_id,
                created_at=created_at_raw,
                created_at_iso=created_at_dt.astimezone(timezone.utc).isoformat(),
                created_at_date=created_at_dt.date().isoformat(),
                first_name=first_name,
                last_name=last_name,
                company_name=company_name,
                display_name=_display_name(first_name, last_name, company_name, customer_id),
                primary_email=str(row["PrimaryEmail"] or ""),
                mobile_phone=str(row["MobilePhone"] or ""),
                is_inactive=is_inactive,
                is_lead=is_lead,
                company_id=row_company_id,
                existing_tags=existing_tags,
            )
        )

    results.sort(key=lambda c: (c.created_at_iso, c.display_name.lower(), c.customer_id))
    return results


def _customer_to_dict(customer: CandidateCustomer, tag_name: str) -> Dict[str, Any]:
    return {
        "customer_id": customer.customer_id,
        "created_at": customer.created_at,
        "created_at_iso": customer.created_at_iso,
        "created_at_date": customer.created_at_date,
        "display_name": customer.display_name,
        "first_name": customer.first_name,
        "last_name": customer.last_name,
        "company_name": customer.company_name,
        "primary_email": customer.primary_email,
        "mobile_phone": customer.mobile_phone,
        "is_inactive": customer.is_inactive,
        "is_lead": customer.is_lead,
        "company_id": customer.company_id,
        "existing_tags": customer.existing_tags,
        "tag_to_add": tag_name,
    }


def write_csv(path: str, customers: Iterable[CandidateCustomer], tag_name: str) -> None:
    fieldnames = [
        "customer_id",
        "display_name",
        "created_at",
        "created_at_date",
        "primary_email",
        "mobile_phone",
        "is_inactive",
        "is_lead",
        "company_id",
        "existing_tags",
        "tag_to_add",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for customer in customers:
            row = _customer_to_dict(customer, tag_name)
            row["existing_tags"] = "|".join(customer.existing_tags)
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def write_json(path: str, customers: Iterable[CandidateCustomer], tag_name: str) -> None:
    payload = [_customer_to_dict(customer, tag_name) for customer in customers]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _extract_tag_names(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return [stripped]
        return _extract_tag_names(parsed)
    if isinstance(value, list):
        names: List[str] = []
        for item in value:
            if isinstance(item, str):
                if item.strip():
                    names.append(item.strip())
            elif isinstance(item, dict):
                name = item.get("Name") or item.get("name")
                if name and str(name).strip():
                    names.append(str(name).strip())
        return names
    if isinstance(value, dict):
        name = value.get("Name") or value.get("name")
        return [str(name).strip()] if name and str(name).strip() else []
    return []


def _merge_tag_names(existing: List[str], tag_name: str) -> List[str]:
    seen: Dict[str, str] = {}
    for value in existing:
        cleaned = value.strip()
        if cleaned:
            seen.setdefault(cleaned.lower(), cleaned)
    if tag_name.strip():
        seen.setdefault(tag_name.strip().lower(), tag_name.strip())
    return list(seen.values())


def _build_customer_update_payload(record: Dict[str, Any], merged_tags: List[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for field in CUSTOMER_UPDATE_FIELDS:
        if field == "tags":
            payload[field] = merged_tags
        else:
            payload[field] = record.get(field)
    return payload


def apply_tags_via_api(
    customers: Iterable[CandidateCustomer],
    *,
    tag_name: str,
    api_base_url: str,
    api_key: str,
    update_path_template: str,
    update_method: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    import httpx  # type: ignore

    if not api_key:
        raise ValueError("SKIMMER_API_KEY or --api-key is required for --apply")

    headers = {
        "skimmer-api-key": api_key,
        "content-type": "application/json",
    }

    updated = 0
    skipped = 0
    errors: List[Dict[str, Any]] = []

    with httpx.Client(base_url=api_base_url.rstrip("/"), headers=headers, timeout=timeout_seconds) as client:
        for customer in customers:
            fetch_path = f"/Customers/{customer.customer_id}"
            update_path = update_path_template.format(customer_id=customer.customer_id)

            try:
                get_response = client.get(fetch_path)
                get_response.raise_for_status()
                record = get_response.json()
            except Exception as exc:
                errors.append(
                    {
                        "customer_id": customer.customer_id,
                        "display_name": customer.display_name,
                        "stage": "fetch",
                        "error": str(exc),
                    }
                )
                continue

            existing_tags = _extract_tag_names(record.get("Tags"))
            if not existing_tags:
                existing_tags = _extract_tag_names(record.get("tags"))
            merged_tags = _merge_tag_names(existing_tags, tag_name)
            if {t.lower() for t in merged_tags} == {t.lower() for t in existing_tags}:
                skipped += 1
                continue

            payload = _build_customer_update_payload(record, merged_tags)

            try:
                response = client.request(update_method, update_path, json=payload)
                response.raise_for_status()
                updated += 1
            except Exception as exc:
                errors.append(
                    {
                        "customer_id": customer.customer_id,
                        "display_name": customer.display_name,
                        "stage": "update",
                        "error": str(exc),
                        "payload": payload,
                    }
                )

    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find Skimmer customers created before a cutoff date and optionally tag them."
    )
    parser.add_argument("--sqlite", help="Path to the Skimmer SQLite export. Defaults to SKIMMER_DB_PATH or the sample DB.")
    parser.add_argument(
        "--cutoff-date",
        required=True,
        help="Reference YYYY-MM-DD date used with --mode. For your current use case: 2025-08-01.",
    )
    parser.add_argument(
        "--mode",
        choices=["before", "since"],
        default="before",
        help="Match customers created before the cutoff date or since it. Default: before",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="Tag name to add to matching customers, for example legacy-pricing.",
    )
    parser.add_argument("--company-id", help="Optional Skimmer CompanyId filter.")
    parser.add_argument("--include-inactive", action="store_true", help="Include inactive customers.")
    parser.add_argument("--include-leads", action="store_true", help="Include leads.")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of matched customers processed/output.")
    parser.add_argument("--csv-out", help="Write matched customers to a CSV file.")
    parser.add_argument("--json-out", help="Write matched customers to a JSON file.")
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Print all matched customers to stdout instead of just the first 25.",
    )
    parser.add_argument("--apply", action="store_true", help="Attempt to apply the tag via the Skimmer API.")
    parser.add_argument("--api-base-url", default=DEFAULT_API_BASE_URL, help="Skimmer API base URL.")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="Skimmer API key.")
    parser.add_argument(
        "--update-path-template",
        default=DEFAULT_UPDATE_PATH_TEMPLATE,
        help="Path template for customer updates. Default: /Customers",
    )
    parser.add_argument(
        "--update-method",
        default=DEFAULT_UPDATE_METHOD,
        help="HTTP method for customer updates. Default: PUT",
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0, help="HTTP timeout for API calls.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cutoff_date = date.fromisoformat(args.cutoff_date)
    except ValueError:
        parser.error("--cutoff-date must be YYYY-MM-DD")

    try:
        sqlite_path = _resolve_sqlite_path(args.sqlite)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    customers = load_candidates(
        sqlite_path,
        cutoff_date=cutoff_date,
        mode=args.mode,
        tag_name=args.tag,
        include_inactive=args.include_inactive,
        include_leads=args.include_leads,
        company_id=args.company_id,
    )

    if args.limit and args.limit > 0:
        customers = customers[: args.limit]

    print(f"SQLite file: {sqlite_path}")
    if args.mode == "before":
        print(f"Cutoff date: {cutoff_date.isoformat()} (customers created before this date match)")
    else:
        print(f"Cutoff date: {cutoff_date.isoformat()} (customers created on or after this date match)")
    print(f"Tag to add: {args.tag}")
    print(f"Matched customers: {len(customers)}")
    print(f"Included inactive: {args.include_inactive}")
    print(f"Included leads: {args.include_leads}")
    if args.company_id:
        print(f"Company filter: {args.company_id}")

    preview = customers if args.show_all else customers[:25]
    if preview:
        print("")
        print("Preview:")
        for customer in preview:
            print(
                f"- {customer.display_name} | {customer.customer_id} | "
                f"created {customer.created_at_date} | tags={', '.join(customer.existing_tags) or '(none)'}"
            )
        if not args.show_all and len(customers) > len(preview):
            print(f"... {len(customers) - len(preview)} more")

    if args.csv_out:
        write_csv(args.csv_out, customers, args.tag)
        print(f"CSV written: {args.csv_out}")

    if args.json_out:
        write_json(args.json_out, customers, args.tag)
        print(f"JSON written: {args.json_out}")

    if not args.apply:
        print("")
        print("Dry run only. Re-run with --apply to attempt API tagging.")
        return 0

    result = apply_tags_via_api(
        customers,
        tag_name=args.tag,
        api_base_url=args.api_base_url,
        api_key=args.api_key,
        update_path_template=args.update_path_template,
        update_method=args.update_method.upper(),
        timeout_seconds=args.timeout_seconds,
    )

    print("")
    print("API apply result:")
    print(f"- updated: {result['updated']}")
    print(f"- skipped: {result['skipped']}")
    print(f"- errors: {len(result['errors'])}")

    if result["errors"]:
        print("")
        print("Errors:")
        for error in result["errors"][:20]:
            print(f"- {json.dumps(error, sort_keys=True)}")
        if len(result["errors"]) > 20:
            print(f"... {len(result['errors']) - 20} more")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
