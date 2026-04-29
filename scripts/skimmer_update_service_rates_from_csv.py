#!/usr/bin/env python3
"""
Update live Skimmer service-location rates from a finalized pricing CSV.

Current pricing use case:
- `sri_042026_price_export.csv` is the source of truth
- column `L` / `final_new_rate` is the approved new service rate
- service locations are keyed by `service_location_id`

Safety rules:
- dry-run by default
- only rows explicitly approved in `approved_for_increase` are eligible
- only `final_new_rate` is used; blank values are skipped
- existing Skimmer `rateType` is preserved from the live API response
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx  # type: ignore


SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "")
DEFAULT_CSV_PATH = os.getenv("PRICE_EXPORT_CSV_PATH", "sri_042026_price_export.csv")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update Skimmer service-location service rates from the finalized pricing CSV."
    )
    parser.add_argument("--csv", default=DEFAULT_CSV_PATH, help=f"Path to pricing CSV. Default: {DEFAULT_CSV_PATH}")
    parser.add_argument("--limit", type=int, default=0, help="Limit eligible service locations after CSV normalization.")
    parser.add_argument(
        "--service-location-id",
        action="append",
        default=[],
        help="Restrict processing to one or more specific service_location_id values.",
    )
    parser.add_argument(
        "--include-unapproved",
        action="store_true",
        help="Allow rows without approved_for_increase=yes. Default behavior is approved rows only.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually update Skimmer. Default is dry run.")
    return parser.parse_args()


def _headers() -> Dict[str, str]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")
    return {
        "skimmer-api-key": SKIMMER_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
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


def money_decimal(value: Any) -> Decimal:
    normalized = normalize_money(value)
    if not normalized:
        raise ValueError(f"Missing money value: {value!r}")
    return Decimal(normalized)


def is_approved(value: Any) -> bool:
    return clean_text(value).lower() in {"1", "true", "yes", "y"}


def load_targets_from_csv(
    csv_path: str,
    *,
    include_unapproved: bool,
    service_location_filter: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    filter_set = {clean_text(value) for value in service_location_filter if clean_text(value)}
    stats = {
        "csv_rows": len(rows),
        "skipped_missing_service_location_id": 0,
        "skipped_unapproved": 0,
        "skipped_missing_final_new_rate": 0,
        "skipped_invalid_rate": 0,
        "skipped_filtered_out": 0,
    }
    grouped: Dict[str, List[Dict[str, str]]] = {}

    for row in rows:
        customer_id = clean_text(row.get("customer_id"))
        if customer_id.upper() == "TOTAL":
            continue

        service_location_id = clean_text(row.get("service_location_id"))
        if not service_location_id:
            stats["skipped_missing_service_location_id"] += 1
            continue

        if filter_set and service_location_id not in filter_set:
            stats["skipped_filtered_out"] += 1
            continue

        if not include_unapproved and not is_approved(row.get("approved_for_increase")):
            stats["skipped_unapproved"] += 1
            continue

        final_new_rate = clean_text(row.get("final_new_rate"))
        if not final_new_rate:
            stats["skipped_missing_final_new_rate"] += 1
            continue

        try:
            normalize_money(final_new_rate)
        except ValueError:
            stats["skipped_invalid_rate"] += 1
            continue

        grouped.setdefault(service_location_id, []).append(row)

    candidates: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    for service_location_id, items in grouped.items():
        rates = sorted({normalize_money(item.get("final_new_rate")) for item in items if clean_text(item.get("final_new_rate"))})
        if len(rates) != 1:
            conflicts.append(
                {
                    "service_location_id": service_location_id,
                    "customer_id": clean_text(items[0].get("customer_id")),
                    "customer": clean_text(items[0].get("full_name")),
                    "action": "skip_conflicting_final_new_rate",
                    "rates": rates,
                }
            )
            continue

        row = items[0]
        candidates.append(
            {
                "service_location_id": service_location_id,
                "customer_id": clean_text(row.get("customer_id")),
                "display_name": clean_text(row.get("full_name")) or service_location_id,
                "new_rate": rates[0],
                "source_rows": len(items),
                "notes": clean_text(row.get("notes")),
            }
        )

    candidates.sort(key=lambda item: (item["display_name"].lower(), item["service_location_id"]))
    conflicts.sort(key=lambda item: (clean_text(item.get("customer")).lower(), clean_text(item.get("service_location_id"))))
    return candidates, conflicts, stats


def skimmer_get(client: httpx.Client, path: str) -> Dict[str, Any]:
    response = client.get(SKIMMER_API_BASE_URL.rstrip("/") + path, headers=_headers())
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def skimmer_put(client: httpx.Client, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    response = client.put(SKIMMER_API_BASE_URL.rstrip("/") + path, headers=_headers(), json=payload)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def maybe_copy(payload: Dict[str, Any], source: Dict[str, Any], key: str) -> None:
    if key in source and source.get(key) is not None:
        payload[key] = source.get(key)


def build_service_location_update_payload(location: Dict[str, Any], new_rate: str) -> Dict[str, Any]:
    # Skimmer's current public docs show PUT /ServiceLocations with a nested
    # serviceRate object. We preserve the live rateType and update only the rate.
    payload: Dict[str, Any] = {
        "id": location.get("id"),
        "address": location.get("address"),
        "city": location.get("city"),
        "state": location.get("state"),
        "zip": location.get("zip"),
        "serviceRate": {
            "rate": float(money_decimal(new_rate)),
            "rateType": clean_text(location.get("rateType")) or "None",
        },
    }

    for key in ("latitude", "longitude", "gateCode", "dogsName", "notes", "locationCode", "minutesAtStop"):
        maybe_copy(payload, location, key)

    return payload


def display_rate(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        return normalize_money(text)
    except ValueError:
        return text


def main() -> int:
    args = parse_args()
    candidates, conflicts, stats = load_targets_from_csv(
        args.csv,
        include_unapproved=args.include_unapproved,
        service_location_filter=args.service_location_id,
    )
    if args.limit and args.limit > 0:
        candidates = candidates[: args.limit]

    summary: Dict[str, Any] = {
        "dry_run": not args.apply,
        "csv_path": args.csv,
        "source_rate_column": "final_new_rate",
        "approved_only": not args.include_unapproved,
        "service_location_filter": [clean_text(value) for value in args.service_location_id if clean_text(value)],
        "limit": args.limit,
        "csv_rows": stats["csv_rows"],
        "eligible_service_locations": len(candidates),
        "csv_conflicts": len(conflicts),
        "skipped_missing_service_location_id": stats["skipped_missing_service_location_id"],
        "skipped_unapproved": stats["skipped_unapproved"],
        "skipped_missing_final_new_rate": stats["skipped_missing_final_new_rate"],
        "skipped_invalid_rate": stats["skipped_invalid_rate"],
        "skipped_filtered_out": stats["skipped_filtered_out"],
        "fetched": 0,
        "would_update": 0,
        "updated": 0,
        "already_at_target": 0,
        "not_found": 0,
        "errors": 0,
        "items": list(conflicts),
    }

    with httpx.Client(timeout=30.0) as client:
        for candidate in candidates:
            service_location_id = candidate["service_location_id"]
            try:
                location = skimmer_get(client, f"/ServiceLocations/{service_location_id}")
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 404:
                    summary["not_found"] += 1
                    summary["items"].append(
                        {
                            "service_location_id": service_location_id,
                            "customer_id": candidate.get("customer_id"),
                            "customer": candidate.get("display_name"),
                            "action": "not_found",
                        }
                    )
                    continue
                summary["errors"] += 1
                summary["items"].append(
                    {
                        "service_location_id": service_location_id,
                        "customer_id": candidate.get("customer_id"),
                        "customer": candidate.get("display_name"),
                        "action": "error_fetch",
                        "error": str(exc),
                    }
                )
                continue
            except Exception as exc:
                summary["errors"] += 1
                summary["items"].append(
                    {
                        "service_location_id": service_location_id,
                        "customer_id": candidate.get("customer_id"),
                        "customer": candidate.get("display_name"),
                        "action": "error_fetch",
                        "error": str(exc),
                    }
                )
                continue

            summary["fetched"] += 1
            current_rate = display_rate(location.get("rate"))
            new_rate = candidate["new_rate"]
            current_rate_type = clean_text(location.get("rateType"))

            item = {
                "service_location_id": service_location_id,
                "customer_id": candidate.get("customer_id"),
                "customer": candidate.get("display_name"),
                "current_rate": current_rate,
                "new_rate": new_rate,
                "rate_type": current_rate_type,
                "source_rows": candidate.get("source_rows"),
            }

            if current_rate == new_rate:
                summary["already_at_target"] += 1
                item["action"] = "already_at_target"
                summary["items"].append(item)
                continue

            payload = build_service_location_update_payload(location, new_rate)
            if not args.apply:
                summary["would_update"] += 1
                item["action"] = "would_update"
                item["payload"] = payload
                summary["items"].append(item)
                continue

            try:
                updated = skimmer_put(client, "/ServiceLocations", payload)
                summary["updated"] += 1
                item["action"] = "updated"
                item["updated_rate"] = display_rate(updated.get("rate")) or new_rate
                summary["items"].append(item)
            except Exception as exc:
                summary["errors"] += 1
                item["action"] = "error_update"
                item["error"] = str(exc)
                item["payload"] = payload
                summary["items"].append(item)

    print(json.dumps(summary, indent=2))
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
