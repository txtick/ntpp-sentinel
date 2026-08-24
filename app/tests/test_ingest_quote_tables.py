import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingest import pipeline


def test_standard_ingest_includes_sales_assist_quote_tables():
    assert "Quote" in pipeline.IMPORT_TABLES
    assert "QuoteLocation" in pipeline.IMPORT_TABLES
    assert "QuoteItem" in pipeline.IMPORT_TABLES


def test_standard_ingest_includes_customer_activity_log_for_labor_fallback():
    assert "CustomerActivityLog" in pipeline.IMPORT_TABLES
    assert "RelatedEntityId" in pipeline.REQUIRED_TABLE_COLUMNS["CustomerActivityLog"]
    assert "CreatedBy" in pipeline.REQUIRED_TABLE_COLUMNS["CustomerActivityLog"]


def test_quote_tables_have_validation_requirements():
    assert pipeline.REQUIRED_TABLE_COLUMNS["Quote"] == ["id", "CustomerId", "Status", "Total", "QuoteDate"]
    assert "QuoteId" in pipeline.REQUIRED_TABLE_COLUMNS["QuoteLocation"]
    assert "QuoteLocationId" in pipeline.REQUIRED_TABLE_COLUMNS["QuoteItem"]


def test_manual_skimmer_import_allowlist_keeps_quotes_recovery_path():
    main_py = (Path(__file__).resolve().parents[1] / "main.py").read_text()

    assert '"quotes"' in main_py
    assert "skimmer_import_quotes" in main_py
