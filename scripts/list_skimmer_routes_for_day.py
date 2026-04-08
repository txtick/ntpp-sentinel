#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo


SKIMMER_API_BASE_URL = os.getenv("SKIMMER_API_BASE_URL", "https://publicapi.getskimmer.com").rstrip("/")
SKIMMER_API_KEY = os.getenv("SKIMMER_API_KEY", "").strip()
TZ_NAME = os.getenv("TIMEZONE", os.getenv("TZ", "America/Chicago"))
RAW_TECH_MAP = os.getenv("SKIMMER_TECH_ID_MAP", "{}").strip() or "{}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List Skimmer routes for a day, including tech IDs and any mapped GHL contact IDs."
    )
    parser.add_argument(
        "--date",
        default="",
        help="Service date in YYYY-MM-DD. Defaults to today in Sentinel timezone.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON response.",
    )
    return parser.parse_args()


def local_today_iso() -> str:
    return datetime.now(tz=ZoneInfo(TZ_NAME)).date().isoformat()


def parse_tech_map() -> dict[str, str]:
    try:
        parsed = json.loads(RAW_TECH_MAP)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


def fetch_routes(service_date: str) -> list[dict]:
    if not SKIMMER_API_KEY:
        raise RuntimeError("SKIMMER_API_KEY is not set")

    query = urllib.parse.urlencode({"ServiceDate": service_date})
    url = f"{SKIMMER_API_BASE_URL}/Routes/GetAllRoutesForDay?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "skimmer-api-key": SKIMMER_API_KEY,
            "Accept": "application/json",
        },
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


def render_routes(routes: list[dict], tech_map: dict[str, str], service_date: str) -> None:
    reverse_map: dict[str, list[str]] = {}
    for ghl_contact_id, tech_id in tech_map.items():
        reverse_map.setdefault(str(tech_id), []).append(str(ghl_contact_id))

    print(f"Skimmer routes for {service_date}")
    print()

    if not routes:
        print("No routes returned.")
        return

    for route in routes:
        tech_id = str(route.get("techId") or "").strip() or "(no tech id)"
        first = str(route.get("techFirstName") or "").strip()
        last = str(route.get("techLastName") or "").strip()
        name = f"{first} {last}".strip() or "(no tech name)"
        stops = route.get("stops") or []
        ghl_ids = reverse_map.get(tech_id, [])
        ghl_label = ", ".join(ghl_ids) if ghl_ids else "-"

        print(f"Tech: {name}")
        print(f"Tech ID: {tech_id}")
        print(f"Mapped GHL Contact IDs: {ghl_label}")
        print(f"Stops: {len(stops)}")

        preview = []
        for stop in stops[:5]:
            customer_first = str(stop.get("customerFirstName") or "").strip()
            customer_last = str(stop.get("customerLastName") or "").strip()
            company = str(stop.get("companyName") or "").strip()
            address = str(stop.get("address") or "").strip()
            who = f"{customer_first} {customer_last}".strip() or company or "(unknown customer)"
            if address:
                who = f"{who} - {address}"
            preview.append(who)

        if preview:
            print("First stops:")
            for item in preview:
                print(f"  - {item}")
        print()


def main() -> int:
    args = parse_args()
    service_date = args.date or local_today_iso()
    tech_map = parse_tech_map()
    routes = fetch_routes(service_date)

    if args.json:
        print(json.dumps(routes, indent=2))
        return 0

    render_routes(routes, tech_map, service_date)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
