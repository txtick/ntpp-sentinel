#!/usr/bin/env python3
"""
Export a CSV of live Skimmer-tagged customers with their current service rate
from the local Skimmer SQLite export and a proposed increased rate.

Source model:
- live Skimmer API determines who is in the cohort via --skimmer-tag
- local Skimmer SQLite export provides ServiceLocation.Rate / RateType

This keeps the cohort current without waiting on the nightly export for tags,
while still using the export for the rate fields you want to hand to pricing.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx  # type: ignore


SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "")
DEFAULT_SQLITE_PATH = os.getenv("SKIMMER_DB_PATH", "/data/skimmer/skimmer.db")
PACKAGE_TAG_ADJUSTMENTS: Dict[str, Decimal] = {
    "patriot": Decimal("55"),
    "freedom": Decimal("40"),
    "liberty": Decimal("20"),
}
EXCLUDED_TAGS = {"not-invoiced"}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export tagged Skimmer customers with current and +15% service rates."
    )
    parser.add_argument("--skimmer-tag", required=True, help="Live Skimmer tag that defines the customer cohort.")
    parser.add_argument("--sqlite", default=DEFAULT_SQLITE_PATH, help="Path to the Skimmer SQLite export.")
    parser.add_argument("--csv-out", required=True, help="Output CSV path.")
    parser.add_argument("--increase-percent", type=Decimal, default=Decimal("15"), help="Percent increase. Default: 15")
    parser.add_argument("--include-inactive", action="store_true", help="Include inactive tagged customers.")
    parser.add_argument("--include-leads", action="store_true", help="Include lead tagged customers.")
    parser.add_argument("--company-id", help="Optional Skimmer CompanyId filter.")
    return parser.parse_args()


def _skimmer_headers() -> Dict[str, str]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")
    return {"skimmer-api-key": SKIMMER_API_KEY, "Accept": "application/json"}


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


def fetch_tagged_customers(
    *,
    skimmer_tag: str,
    include_inactive: bool,
    include_leads: bool,
    company_id: Optional[str],
) -> List[Dict[str, Any]]:
    target_tag_lc = skimmer_tag.strip().lower()
    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            SKIMMER_API_BASE_URL.rstrip("/") + "/Customers",
            headers=_skimmer_headers(),
        )
        response.raise_for_status()
        data = response.json()

    customers = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    results: List[Dict[str, Any]] = []
    for customer in customers:
        tags = _extract_tag_names(customer.get("tags"))
        if target_tag_lc not in {tag.lower() for tag in tags}:
            continue
        if EXCLUDED_TAGS & {tag.lower() for tag in tags}:
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
        display_name = " ".join(part for part in [first_name, last_name] if part).strip() or company_name or clean_text(customer.get("id"))

        results.append(
            {
                "customer_id": clean_text(customer.get("id")),
                "display_name": display_name,
                "first_name": first_name,
                "last_name": last_name,
                "company_name": company_name,
                "primary_email": clean_text(customer.get("primaryEmail")),
                "mobile_phone": clean_text(customer.get("mobilePhone")),
                "is_inactive": is_inactive,
                "is_lead": is_lead,
                "company_id": row_company_id,
                "created_at": clean_text(customer.get("createdAt")),
                "tags": tags,
            }
        )

    results.sort(key=lambda row: (row["display_name"].lower(), row["customer_id"]))
    return results


def load_service_locations(sqlite_path: str) -> Dict[str, List[Dict[str, Any]]]:
    if not Path(sqlite_path).is_file():
        raise FileNotFoundError(f"SQLite file not found: {sqlite_path}")

    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT
                id,
                CustomerId,
                Address,
                City,
                State,
                Zip,
                Rate,
                RateType,
                LaborCost,
                LaborCostType,
                Notes,
                CompanyId
            FROM ServiceLocation
            WHERE COALESCE(Deleted, 0) = 0
            """
        ).fetchall()
    finally:
        conn.close()

    by_customer: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        customer_id = clean_text(row["CustomerId"])
        if not customer_id:
            continue
        by_customer.setdefault(customer_id, []).append(dict(row))

    for locations in by_customer.values():
        locations.sort(key=lambda row: (clean_text(row.get("Address")).lower(), clean_text(row.get("id"))))
    return by_customer


def calculate_increased_rate(current_rate: Any, increase_percent: Decimal) -> str:
    if current_rate is None or str(current_rate).strip() == "":
        return ""
    current = Decimal(str(current_rate))
    multiplier = Decimal("1") + (increase_percent / Decimal("100"))
    increased = (current * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(increased, "f")


def current_rate_str(current_rate: Any) -> str:
    if current_rate is None or str(current_rate).strip() == "":
        return ""
    current = Decimal(str(current_rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(current, "f")


def package_adjustment(tags: List[str]) -> Decimal:
    matched = [PACKAGE_TAG_ADJUSTMENTS[tag.lower()] for tag in tags if tag.lower() in PACKAGE_TAG_ADJUSTMENTS]
    return sum(matched, Decimal("0"))


def package_tags_used(tags: List[str]) -> List[str]:
    return sorted({tag for tag in tags if tag.lower() in PACKAGE_TAG_ADJUSTMENTS})


def calculate_adjusted_rates(current_rate: Any, tags: List[str], increase_percent: Decimal) -> Dict[str, str]:
    if current_rate is None or str(current_rate).strip() == "":
        return {
            "current_service_rate": "",
            "current_base_rate": "",
            "increased_base_rate": "",
            "projected_new_total_rate": "",
        }

    current = Decimal(str(current_rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    adjustment = package_adjustment(tags)
    applied_package_tags = package_tags_used(tags)
    base = (current - adjustment).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    multiplier = Decimal("1") + (increase_percent / Decimal("100"))
    increased_base = (base * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    projected_total = (increased_base + adjustment).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return {
        "current_service_rate": format(current, "f"),
        "package_adjustment_applied": format(adjustment.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f"),
        "package_tag_used": "|".join(applied_package_tags),
        "current_base_rate": format(base, "f"),
        "increased_base_rate": format(increased_base, "f"),
        "projected_new_total_rate": format(projected_total, "f"),
    }


def write_csv(
    csv_path: str,
    tagged_customers: List[Dict[str, Any]],
    service_locations_by_customer: Dict[str, List[Dict[str, Any]]],
    increase_percent: Decimal,
) -> int:
    fieldnames = [
        "customer_id",
        "service_location_id",
        "full_name",
        "city",
        "zip_code",
        "current_service_rate",
        "package_adjustment_applied",
        "package_tag_used",
        "current_base_rate",
        "increased_base_rate",
        "projected_new_total_rate",
        "approved_for_increase",
        "final_new_rate",
        "notes",
    ]

    row_count = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for customer in tagged_customers:
            locations = service_locations_by_customer.get(customer["customer_id"], [])
            if not locations:
                rates = calculate_adjusted_rates(None, customer["tags"], increase_percent)
                writer.writerow(
                    {
                        "customer_id": customer["customer_id"],
                        "service_location_id": "",
                        "full_name": customer["display_name"],
                        "city": "",
                        "zip_code": "",
                        **rates,
                        "approved_for_increase": "",
                        "final_new_rate": "",
                        "notes": "",
                    }
                )
                row_count += 1
                continue

            for location in locations:
                rates = calculate_adjusted_rates(location.get("Rate"), customer["tags"], increase_percent)
                writer.writerow(
                    {
                        "customer_id": customer["customer_id"],
                        "service_location_id": clean_text(location.get("id")),
                        "full_name": customer["display_name"],
                        "city": clean_text(location.get("City")),
                        "zip_code": clean_text(location.get("Zip")),
                        **rates,
                        "approved_for_increase": "",
                        "final_new_rate": "",
                        "notes": "",
                    }
                )
                row_count += 1

    return row_count


def main() -> int:
    args = parse_args()

    tagged_customers = fetch_tagged_customers(
        skimmer_tag=args.skimmer_tag,
        include_inactive=args.include_inactive,
        include_leads=args.include_leads,
        company_id=args.company_id,
    )
    service_locations_by_customer = load_service_locations(args.sqlite)
    row_count = write_csv(
        args.csv_out,
        tagged_customers,
        service_locations_by_customer,
        args.increase_percent,
    )

    print(f"Tagged customers fetched from Skimmer API: {len(tagged_customers)}")
    print(f"CSV rows written: {row_count}")
    print(f"CSV path: {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
