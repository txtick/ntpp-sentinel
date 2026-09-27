import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services import dashboard_backend


def test_jarrett_commission_defaults_start_fresh():
    assert dashboard_backend.JARRETT_COMMISSION_CUTOFF == date(2026, 9, 27)
    assert dashboard_backend.JARRETT_REPAIR_MARGIN_RATE == 0.08
    assert dashboard_backend.JARRETT_FILTER_CLEAN_RATE == 8


def test_cleaning_tech_filter_clean_rate_is_unchanged():
    assert dashboard_backend.LABOR_FILTER_CLEAN_RATE == 25
