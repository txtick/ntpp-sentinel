#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from typing import Any


SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com").rstrip("/")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "").strip()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare live Skimmer route-stop counts by tech over a date range."
    )
    parser.add_argument("--start", required=True, help="Start date in YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date in YYYY-MM-DD")
    parser.add_argument(
        "--debug-first-day",
        action="store_true",
        help="Print sample stop keys from the first returned route for inspection.",
    )
    return parser.parse_args()


def _parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)


def _daterange(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _fetch_routes(service_date: str) -> list[dict[str, Any]]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")

    query = urllib.parse.urlencode({"ServiceDate": service_date})
    url = f"{SKIMMER_API_BASE_URL}/Routes/GetAllRoutesForDay?{query}"
    req = urllib.request.Request(
        url,
        headers={"skimmer-api-key": SKIMMER_API_KEY, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Skimmer API error {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Skimmer API request failed: {exc}") from exc

    try:
        data = json.loads(body)
    except Exception as exc:
        raise RuntimeError("Skimmer API returned invalid JSON") from exc
    if not isinstance(data, list):
        raise RuntimeError("Unexpected response shape from Skimmer routes API")
    return data


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in {"true", "1", "yes"}:
            return True
        if token in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _first_present(mapping: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping.get(key)
    return None


def main() -> int:
    args = _parse_args()
    start = _parse_iso_date(args.start)
    end = _parse_iso_date(args.end)
    if start > end:
        raise RuntimeError("start must be on or before end")

    totals: dict[str, dict[str, Any]] = {}
    first_day_sample: tuple[str, list[dict[str, Any]]] | None = None

    for service_day in _daterange(start, end):
        day_iso = service_day.isoformat()
        routes = _fetch_routes(day_iso)
        if first_day_sample is None and routes:
            first_day_sample = (day_iso, routes)

        for route in routes:
            tech_id = str(route.get("techId") or "").strip() or "(no tech id)"
            first = str(route.get("techFirstName") or "").strip()
            last = str(route.get("techLastName") or "").strip()
            tech_name = f"{first} {last}".strip() or "(no tech name)"
            bucket = totals.setdefault(
                tech_id,
                {
                    "tech_name": tech_name,
                    "tech_id": tech_id,
                    "all_stops": 0,
                    "non_skipped_stops": 0,
                    "completed_stops": 0,
                    "completed_non_skipped_stops": 0,
                    "days_with_routes": set(),
                },
            )
            bucket["days_with_routes"].add(day_iso)

            for stop in route.get("stops") or []:
                bucket["all_stops"] += 1

                skipped = _coerce_bool(
                    _first_present(
                        stop,
                        ["isSkipped", "skipped", "isStopSkipped"],
                    )
                )
                completed = _coerce_bool(
                    _first_present(
                        stop,
                        ["isCompleted", "completed", "isComplete"],
                    )
                )
                complete_time = _first_present(
                    stop,
                    ["completeTime", "completedAt", "completeDate", "endTime"],
                )
                if completed is None:
                    completed = bool(complete_time)
                if skipped is None:
                    skipped = False

                if not skipped:
                    bucket["non_skipped_stops"] += 1
                if completed:
                    bucket["completed_stops"] += 1
                if completed and not skipped:
                    bucket["completed_non_skipped_stops"] += 1

    if args.debug_first_day and first_day_sample:
        sample_day, sample_routes = first_day_sample
        print(f"# sample_day\t{sample_day}")
        first_route = sample_routes[0]
        print("# first_route_keys\t" + ",".join(sorted(first_route.keys())))
        first_stops = first_route.get("stops") or []
        if first_stops:
            print("# first_stop_keys\t" + ",".join(sorted(first_stops[0].keys())))

    headers = [
        "tech_name",
        "tech_id",
        "all_stops",
        "non_skipped_stops",
        "completed_stops",
        "completed_non_skipped_stops",
        "days_with_routes",
    ]
    print("\t".join(headers))
    for row in sorted(totals.values(), key=lambda item: (str(item["tech_name"]), str(item["tech_id"]))):
        print(
            "\t".join(
                [
                    str(row["tech_name"]),
                    str(row["tech_id"]),
                    str(row["all_stops"]),
                    str(row["non_skipped_stops"]),
                    str(row["completed_stops"]),
                    str(row["completed_non_skipped_stops"]),
                    str(len(row["days_with_routes"])),
                ]
            )
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
