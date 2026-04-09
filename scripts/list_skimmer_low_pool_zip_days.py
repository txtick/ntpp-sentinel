#!/usr/bin/env python3
import argparse
import sqlite3
import sys
from pathlib import Path


DEFAULT_SQLITE_PATH = "3f19c6b0c1ef4a1d876f942348997106.db"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List service dates for a specific Skimmer tech where the tech has fewer "
            "than the requested number of pools, along with the zip codes serviced "
            "on those days."
        )
    )
    parser.add_argument(
        "--sqlite",
        default=DEFAULT_SQLITE_PATH,
        help=f"Path to the Skimmer SQLite export. Default: {DEFAULT_SQLITE_PATH}",
    )
    parser.add_argument(
        "--tech",
        required=True,
        help="Tech name to match. Partial matches are allowed.",
    )
    parser.add_argument(
        "--max-pools",
        type=int,
        default=9,
        help="Maximum pool count to include. Default: 9 (<10 pools).",
    )
    parser.add_argument(
        "--start-date",
        default="",
        help="Optional start date filter in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        default="",
        help="Optional end date filter in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        help="Print CSV instead of a human-readable table.",
    )
    return parser.parse_args()


def connect_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def find_matching_techs(conn: sqlite3.Connection, tech_query: str) -> list[sqlite3.Row]:
    sql = """
        SELECT
            id,
            TRIM(COALESCE(FirstName, '') || ' ' || COALESCE(LastName, '')) AS tech_name,
            Username,
            RoleType,
            IsActive
        FROM Account
        WHERE LOWER(TRIM(COALESCE(FirstName, '') || ' ' || COALESCE(LastName, ''))) LIKE LOWER(?)
        ORDER BY tech_name
    """
    return conn.execute(sql, (f"%{tech_query.strip()}%",)).fetchall()


def fetch_low_pool_days(
    conn: sqlite3.Connection,
    account_id: str,
    max_pools: int,
    start_date: str,
    end_date: str,
) -> list[sqlite3.Row]:
    filters = [
        "COALESCE(rs.Deleted, 0) = 0",
        "COALESCE(rs.IsSkipped, 0) = 0",
        "rs.AccountId = ?",
    ]
    params: list[object] = [account_id]

    if start_date:
        filters.append("date(rs.ServiceDate) >= date(?)")
        params.append(start_date)
    if end_date:
        filters.append("date(rs.ServiceDate) <= date(?)")
        params.append(end_date)

    sql = f"""
        WITH day_rollup AS (
            SELECT
                date(rs.ServiceDate) AS service_date,
                COUNT(DISTINCT ss.PoolId) AS pool_count,
                COUNT(DISTINCT rs.ServiceLocationId) AS stop_count
            FROM RouteStop rs
            LEFT JOIN ServiceStop ss
                ON ss.RouteStopId = rs.id
               AND COALESCE(ss.Deleted, 0) = 0
            WHERE {' AND '.join(filters)}
            GROUP BY date(rs.ServiceDate)
            HAVING COUNT(DISTINCT ss.PoolId) <= ?
        )
        SELECT
            d.service_date,
            d.pool_count,
            d.stop_count,
            GROUP_CONCAT(DISTINCT sl.Zip) AS zip_codes
        FROM day_rollup d
        JOIN RouteStop rs
            ON date(rs.ServiceDate) = d.service_date
           AND rs.AccountId = ?
           AND COALESCE(rs.Deleted, 0) = 0
           AND COALESCE(rs.IsSkipped, 0) = 0
        LEFT JOIN ServiceLocation sl
            ON sl.id = rs.ServiceLocationId
           AND COALESCE(sl.Deleted, 0) = 0
        GROUP BY d.service_date, d.pool_count, d.stop_count
        ORDER BY d.service_date
    """
    params.append(max_pools)
    params.append(account_id)
    return conn.execute(sql, params).fetchall()


def render_table(tech_name: str, rows: list[sqlite3.Row], max_pools: int) -> None:
    print(f"Tech: {tech_name}")
    print(f"Days with {max_pools} pools or fewer")
    print()

    if not rows:
        print("No matching days found.")
        return

    date_width = len("service_date")
    pool_width = len("pool_count")
    stop_width = len("stop_count")

    for row in rows:
        date_width = max(date_width, len(str(row["service_date"] or "")))
        pool_width = max(pool_width, len(str(row["pool_count"])))
        stop_width = max(stop_width, len(str(row["stop_count"])))

    header = (
        f"{'service_date':<{date_width}}  "
        f"{'pool_count':>{pool_width}}  "
        f"{'stop_count':>{stop_width}}  "
        "zip_codes"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{str(row['service_date'] or ''):<{date_width}}  "
            f"{row['pool_count']:>{pool_width}}  "
            f"{row['stop_count']:>{stop_width}}  "
            f"{row['zip_codes'] or ''}"
        )


def render_csv(rows: list[sqlite3.Row]) -> None:
    print("service_date,pool_count,stop_count,zip_codes")
    for row in rows:
        zip_codes = (row["zip_codes"] or "").replace('"', '""')
        print(f'{row["service_date"]},{row["pool_count"]},{row["stop_count"]},"{zip_codes}"')


def main() -> int:
    args = parse_args()
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.exists():
        print(f"ERROR: SQLite file not found: {sqlite_path}", file=sys.stderr)
        return 1

    conn = connect_sqlite(str(sqlite_path))
    techs = find_matching_techs(conn, args.tech)
    if not techs:
        print(f'ERROR: No tech found matching "{args.tech}"', file=sys.stderr)
        return 1
    if len(techs) > 1:
        print(f'ERROR: Multiple techs matched "{args.tech}". Be more specific:', file=sys.stderr)
        for tech in techs:
            print(
                f'  - {tech["tech_name"]} '
                f'(username={tech["Username"]}, role={tech["RoleType"]}, active={tech["IsActive"]})',
                file=sys.stderr,
            )
        return 1

    tech = techs[0]
    rows = fetch_low_pool_days(
        conn=conn,
        account_id=tech["id"],
        max_pools=args.max_pools,
        start_date=args.start_date,
        end_date=args.end_date,
    )

    if args.csv:
        render_csv(rows)
    else:
        render_table(tech["tech_name"], rows, args.max_pools)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
