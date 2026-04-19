#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare raw Skimmer SQLite labor-count candidates for a date range."
    )
    parser.add_argument("--sqlite", required=True, help="Path to the Skimmer SQLite db file")
    parser.add_argument("--start", required=True, help="Start date in YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date in YYYY-MM-DD")
    return parser.parse_args()


def _validate_iso_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def main() -> None:
    args = _parse_args()
    db_path = Path(args.sqlite)
    if not db_path.exists():
        raise SystemExit(f"SQLite db not found: {db_path}")

    start = _validate_iso_date(args.start)
    end = _validate_iso_date(args.end) + " 23:59:59"

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            MAX(substr(ServiceDate, 1, 10)) AS max_route_stop_service_date,
            MAX(substr(ServiceDate, 1, 10)) FILTER (WHERE CompleteTime IS NOT NULL) AS max_completed_route_stop_service_date
        FROM RouteStop
        """
    )
    route_meta = cur.fetchone()

    cur.execute(
        """
        SELECT
            MAX(substr(ServiceDate, 1, 10)) AS max_work_order_service_date,
            MAX(substr(ServiceDate, 1, 10)) FILTER (WHERE CompleteTime IS NOT NULL) AS max_completed_work_order_service_date
        FROM WorkOrder
        """
    )
    work_order_meta = cur.fetchone()

    query = """
    WITH route_candidates AS (
        SELECT
            a.id AS tech_id,
            a.FirstName || ' ' || a.LastName AS tech_name,
            COUNT(*) FILTER (
                WHERE COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
            ) AS route_non_skipped,
            COUNT(*) FILTER (
                WHERE COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND rs.CompleteTime IS NOT NULL
            ) AS route_completed,
            COUNT(*) FILTER (
                WHERE COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND rs.RouteAssignmentId IS NOT NULL
                  AND rs.RouteAssignmentId <> ''
            ) AS route_assigned_non_skipped,
            COUNT(*) FILTER (
                WHERE COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND rs.RouteAssignmentId IS NOT NULL
                  AND rs.RouteAssignmentId <> ''
                  AND rs.CompleteTime IS NOT NULL
            ) AS route_assigned_completed,
            COUNT(DISTINCT rs.ServiceLocationId || '|' || substr(rs.ServiceDate, 1, 10)) FILTER (
                WHERE COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND rs.RouteAssignmentId IS NOT NULL
                  AND rs.RouteAssignmentId <> ''
                  AND rs.CompleteTime IS NOT NULL
            ) AS route_assigned_completed_distinct_location_day,
            COUNT(*) FILTER (
                WHERE COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 1
            ) AS skipped_stops
        FROM RouteStop rs
        JOIN Account a ON a.id = rs.AccountId
        WHERE rs.ServiceDate >= ? AND rs.ServiceDate <= ?
        GROUP BY a.id, tech_name
    ),
    service_stop_candidates AS (
        SELECT
            a.id AS tech_id,
            COUNT(*) FILTER (
                WHERE COALESCE(ss.Deleted, 0) = 0
                  AND COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
            ) AS service_stops,
            COUNT(DISTINCT rs.id) FILTER (
                WHERE COALESCE(ss.Deleted, 0) = 0
                  AND COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
            ) AS distinct_route_stops_with_service_stops
        FROM ServiceStop ss
        JOIN RouteStop rs ON rs.id = ss.RouteStopId
        JOIN Account a ON a.id = rs.AccountId
        WHERE rs.ServiceDate >= ? AND rs.ServiceDate <= ?
        GROUP BY a.id
    ),
    service_stop_entry_candidates AS (
        SELECT
            a.id AS tech_id,
            COUNT(DISTINCT ss.id) FILTER (
                WHERE COALESCE(ss.Deleted, 0) = 0
                  AND COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND COALESCE(sse.Deleted, 0) = 0
            ) AS service_stops_with_entries,
            COUNT(DISTINCT ss.id) FILTER (
                WHERE COALESCE(ss.Deleted, 0) = 0
                  AND COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND COALESCE(sse.Deleted, 0) = 0
                  AND (sse.WorkOrderId IS NULL OR sse.WorkOrderId = '')
            ) AS service_stops_with_non_work_order_entries,
            COUNT(DISTINCT rs.id) FILTER (
                WHERE COALESCE(ss.Deleted, 0) = 0
                  AND COALESCE(rs.Deleted, 0) = 0
                  AND COALESCE(rs.IsSkipped, 0) = 0
                  AND COALESCE(sse.Deleted, 0) = 0
                  AND (sse.WorkOrderId IS NULL OR sse.WorkOrderId = '')
            ) AS route_stops_with_non_work_order_entries
        FROM ServiceStopEntry sse
        JOIN ServiceStop ss ON ss.id = sse.ServiceStopId
        JOIN RouteStop rs ON rs.id = ss.RouteStopId
        JOIN Account a ON a.id = rs.AccountId
        WHERE rs.ServiceDate >= ? AND rs.ServiceDate <= ?
        GROUP BY a.id
    ),
    work_order_candidates AS (
        SELECT
            a.id AS tech_id,
            COUNT(*) FILTER (
                WHERE COALESCE(w.Deleted, 0) = 0
                  AND w.CompleteTime IS NOT NULL
            ) AS work_orders_completed,
            COUNT(*) FILTER (
                WHERE COALESCE(w.Deleted, 0) = 0
                  AND w.CompleteTime IS NOT NULL
                  AND (
                      lower(COALESCE(wt.Description, '')) = 'filter clean'
                      OR lower(COALESCE(w.WorkNeeded, '')) LIKE '%filter clean%'
                  )
            ) AS filter_cleans_completed
        FROM WorkOrder w
        JOIN Account a ON a.id = w.AccountId
        LEFT JOIN WorkOrderType wt ON wt.id = w.WorkOrderTypeId
        WHERE w.ServiceDate >= ? AND w.ServiceDate <= ?
        GROUP BY a.id
    )
    SELECT
        rc.tech_name,
        rc.tech_id,
        rc.route_non_skipped,
        rc.route_completed,
        rc.route_assigned_non_skipped,
        rc.route_assigned_completed,
        rc.route_assigned_completed_distinct_location_day,
        COALESCE(ssc.service_stops, 0) AS service_stops,
        COALESCE(ssc.distinct_route_stops_with_service_stops, 0) AS distinct_route_stops_with_service_stops,
        COALESCE(ssec.service_stops_with_entries, 0) AS service_stops_with_entries,
        COALESCE(ssec.service_stops_with_non_work_order_entries, 0) AS service_stops_with_non_work_order_entries,
        COALESCE(ssec.route_stops_with_non_work_order_entries, 0) AS route_stops_with_non_work_order_entries,
        COALESCE(woc.work_orders_completed, 0) AS work_orders_completed,
        COALESCE(woc.filter_cleans_completed, 0) AS filter_cleans_completed,
        rc.skipped_stops
    FROM route_candidates rc
    LEFT JOIN service_stop_candidates ssc ON ssc.tech_id = rc.tech_id
    LEFT JOIN service_stop_entry_candidates ssec ON ssec.tech_id = rc.tech_id
    LEFT JOIN work_order_candidates woc ON woc.tech_id = rc.tech_id
    ORDER BY rc.tech_name ASC
    """

    cur.execute(query, [start, end, start, end, start, end, start, end])
    rows = cur.fetchall()
    conn.close()

    headers = [
        "tech_name",
        "tech_id",
        "route_non_skipped",
        "route_completed",
        "route_assigned_non_skipped",
        "route_assigned_completed",
        "route_assigned_completed_distinct_location_day",
        "service_stops",
        "distinct_route_stops_with_service_stops",
        "service_stops_with_entries",
        "service_stops_with_non_work_order_entries",
        "route_stops_with_non_work_order_entries",
        "work_orders_completed",
        "filter_cleans_completed",
        "skipped_stops",
    ]

    print(
        "\t".join(
            [
                "db_max_route_stop_service_date",
                "" if route_meta[0] is None else str(route_meta[0]),
                "db_max_completed_route_stop_service_date",
                "" if route_meta[1] is None else str(route_meta[1]),
                "db_max_work_order_service_date",
                "" if work_order_meta[0] is None else str(work_order_meta[0]),
                "db_max_completed_work_order_service_date",
                "" if work_order_meta[1] is None else str(work_order_meta[1]),
            ]
        )
    )
    print("\t".join(headers))
    for row in rows:
        print("\t".join("" if value is None else str(value) for value in row))


if __name__ == "__main__":
    main()
