#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import date

from pg import pg


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare live labor-count candidates from normalized Skimmer data."
    )
    parser.add_argument("--start", required=True, help="Start date in YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date in YYYY-MM-DD")
    return parser.parse_args()


def _validate_iso_date(value: str) -> str:
    date.fromisoformat(value)
    return value


def main() -> None:
    args = _parse_args()
    start = _validate_iso_date(args.start)
    end = _validate_iso_date(args.end)

    with pg() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH base_route_stops AS (
                    SELECT
                        t.source_account_id AS tech_id,
                        COALESCE(NULLIF(trim(concat_ws(' ', t.first_name, t.last_name)), ''), NULLIF(t.username, ''), t.source_account_id) AS tech_name,
                        s.source_route_stop_id,
                        s.source_service_location_id,
                        s.source_route_assignment_id,
                        s.service_date,
                        s.complete_time,
                        s.is_skipped
                    FROM technician_route_stops s
                    JOIN technicians t ON t.id = s.technician_id
                    WHERE t.source_system = 'skimmer'
                      AND s.technician_id IS NOT NULL
                      AND s.service_date::date BETWEEN %s AND %s
                ),
                route_candidates AS (
                    SELECT
                        tech_id,
                        tech_name,
                        COUNT(*) FILTER (WHERE is_skipped = FALSE) AS route_non_skipped,
                        COUNT(*) FILTER (
                            WHERE is_skipped = FALSE
                              AND complete_time IS NOT NULL
                        ) AS route_completed,
                        COUNT(*) FILTER (
                            WHERE is_skipped = FALSE
                              AND source_route_assignment_id IS NOT NULL
                              AND source_route_assignment_id <> ''
                        ) AS route_assigned_non_skipped,
                        COUNT(*) FILTER (
                            WHERE is_skipped = FALSE
                              AND source_route_assignment_id IS NOT NULL
                              AND source_route_assignment_id <> ''
                              AND complete_time IS NOT NULL
                        ) AS route_assigned_completed,
                        COUNT(DISTINCT source_service_location_id || '|' || service_date::date::text) FILTER (
                            WHERE is_skipped = FALSE
                              AND source_route_assignment_id IS NOT NULL
                              AND source_route_assignment_id <> ''
                              AND complete_time IS NOT NULL
                        ) AS route_assigned_completed_distinct_location_day,
                        COUNT(*) FILTER (WHERE is_skipped = TRUE) AS skipped_stops
                    FROM base_route_stops
                    GROUP BY tech_id, tech_name
                ),
                chemistry_candidates AS (
                    SELECT
                        t.source_account_id AS tech_id,
                        COUNT(DISTINCT r.source_service_stop_id) AS chemistry_route_stop_count,
                        COUNT(DISTINCT r.pool_id::text || '|' || r.service_date::date::text) AS chemistry_pool_day_count
                    FROM chemistry_readings r
                    JOIN technician_route_stops s
                      ON s.source_system = r.source_system
                     AND s.source_route_stop_id = r.source_service_stop_id
                    JOIN technicians t ON t.id = s.technician_id
                    WHERE t.source_system = 'skimmer'
                      AND r.service_date::date BETWEEN %s AND %s
                      AND s.is_skipped = FALSE
                    GROUP BY t.source_account_id
                ),
                work_order_candidates AS (
                    SELECT
                        t.source_account_id AS tech_id,
                        COUNT(*) FILTER (
                            WHERE COALESCE(w.is_deleted, FALSE) = FALSE
                              AND w.complete_time IS NOT NULL
                        ) AS work_orders_completed,
                        COUNT(*) FILTER (
                            WHERE COALESCE(w.is_deleted, FALSE) = FALSE
                              AND w.complete_time IS NOT NULL
                              AND (
                                  lower(COALESCE(wt.description, '')) = 'filter clean'
                                  OR lower(COALESCE(w.work_needed, '')) LIKE '%%filter clean%%'
                              )
                        ) AS filter_cleans_completed
                    FROM sk_work_order w
                    JOIN technicians t
                      ON t.source_system = w.source_system
                     AND t.source_account_id = w.source_account_id
                    LEFT JOIN sk_work_order_type wt
                      ON wt.source_system = w.source_system
                     AND wt.source_work_order_type_id = w.source_work_order_type_id
                    WHERE t.source_system = 'skimmer'
                      AND w.service_date::date BETWEEN %s AND %s
                    GROUP BY t.source_account_id
                )
                SELECT
                    rc.tech_name,
                    rc.tech_id,
                    rc.route_non_skipped,
                    rc.route_completed,
                    rc.route_assigned_non_skipped,
                    rc.route_assigned_completed,
                    rc.route_assigned_completed_distinct_location_day,
                    COALESCE(cc.chemistry_route_stop_count, 0) AS chemistry_route_stop_count,
                    COALESCE(cc.chemistry_pool_day_count, 0) AS chemistry_pool_day_count,
                    COALESCE(wc.work_orders_completed, 0) AS work_orders_completed,
                    COALESCE(wc.filter_cleans_completed, 0) AS filter_cleans_completed,
                    rc.skipped_stops
                FROM route_candidates rc
                LEFT JOIN chemistry_candidates cc ON cc.tech_id = rc.tech_id
                LEFT JOIN work_order_candidates wc ON wc.tech_id = rc.tech_id
                ORDER BY rc.tech_name ASC
                """,
                [start, end, start, end, start, end],
            )
            rows = cur.fetchall()

    headers = [
        "tech_name",
        "tech_id",
        "route_non_skipped",
        "route_completed",
        "route_assigned_non_skipped",
        "route_assigned_completed",
        "route_assigned_completed_distinct_location_day",
        "chemistry_route_stop_count",
        "chemistry_pool_day_count",
        "work_orders_completed",
        "filter_cleans_completed",
        "skipped_stops",
    ]

    print("\t".join(headers))
    for row in rows:
        print("\t".join(str(row.get(header, "")) for header in headers))


if __name__ == "__main__":
    main()
