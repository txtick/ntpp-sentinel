import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import dashboard_backend


def test_jarrett_commission_defaults_start_fresh():
    assert dashboard_backend.JARRETT_COMMISSION_CUTOFF == date(2026, 9, 27)
    assert dashboard_backend.JARRETT_REPAIR_MARGIN_RATE == 0.08
    assert dashboard_backend.JARRETT_FILTER_CLEAN_RATE == 8


def test_cleaning_tech_filter_clean_rate_is_unchanged():
    assert dashboard_backend.LABOR_FILTER_CLEAN_RATE == 25


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return self.rows


def _job_row(**overrides):
    row = {
        "source_work_order_id": "wo-1",
        "service_date": date(2026, 10, 6),
        "source_quote_id": "quote-1",
        "quote_match_count": 1,
        "subtotal_amount": 1000,
        "total_cost": 400,
        "commission_amount": 48,
        "invoiced_subtotal": 1000,
        "payment_status": "Paid",
        "amount_due": 0,
        "payment_date": datetime(2026, 10, 5, 18, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


def test_jarrett_job_paid_before_completion_is_payable_when_completed():
    cur = _Cursor([_job_row()])
    result = dashboard_backend._jarrett_repair_commissions(cur, date(2026, 10, 4), date(2026, 10, 10))
    assert len(result["payable"]) == 1
    assert result["payable_total"] == 48
    assert "workorderfinished" in cur.sql.lower()
    assert "string_agg" in cur.sql.lower()


def test_jarrett_split_invoice_job_stays_outstanding_until_fully_invoiced():
    cur = _Cursor([_job_row(invoiced_subtotal=500)])
    result = dashboard_backend._jarrett_repair_commissions(cur, date(2026, 10, 4), date(2026, 10, 10))
    assert not result["payable"]
    assert len(result["outstanding"]) == 1


def test_jarrett_filter_cleans_are_excluded_from_repair_commission_sql():
    cur = _Cursor([])
    dashboard_backend._jarrett_repair_commissions(cur, date(2026, 10, 4), date(2026, 10, 10))
    assert "<> 'filter clean'" in cur.sql.lower()
