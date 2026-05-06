"""
Tests for the chemical invoice ingestion service.

Run inside the Docker container:
    docker exec -it ntpp-sentinel python -m pytest app/tests/test_chemical_invoices.py -v

Or from the repo root (if pytest is available locally):
    cd app && python -m pytest tests/test_chemical_invoices.py -v

All tests use an in-memory SQLite database; no network calls, no pdfplumber,
no OpenAI key required.
"""

import asyncio
import json
import sqlite3
import sys
import os
import pytest

# Allow importing from app/ when running from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Patch db.DB_PATH before importing the service so every db() call
# in this test session hits an in-memory database.
import db as db_module

_TEST_DB_PATH = ":memory:"
db_module.DB_PATH = _TEST_DB_PATH
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("DB_PATH", "/tmp/ntpp-chemical-invoice-test.sqlite")
os.environ.setdefault("AI_GATE_ENABLED", "0")


def _fresh_conn() -> sqlite3.Connection:
    """Return a connected, schema-initialised in-memory DB."""
    conn = sqlite3.connect(_TEST_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # Bootstrap all tables
    db_module.init_db.__globals__["DB_PATH"] = _TEST_DB_PATH
    _create_chem_tables(conn)
    return conn


def _create_chem_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS chemical_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_sha256 TEXT NOT NULL UNIQUE,
            vendor_name TEXT NOT NULL,
            branch_location TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            account_number TEXT,
            po_number TEXT,
            ordered_by TEXT,
            invoice_total REAL,
            subtotal REAL,
            tax REAL,
            fees REAL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'PENDING',
            reconciliation_json TEXT,
            extraction_mode TEXT,
            ingested_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            pdf_path TEXT,
            UNIQUE(vendor_name, invoice_number)
        );

        CREATE TABLE IF NOT EXISTS chemical_invoice_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER NOT NULL,
            line_number INTEGER,
            item_code TEXT,
            raw_description TEXT NOT NULL,
            normalized_product_name TEXT,
            category TEXT,
            cost_type TEXT,
            quantity REAL,
            unit TEXT,
            default_unit TEXT,
            unit_price REAL,
            extended_price REAL,
            confidence REAL,
            alias_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS chemical_product_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_name TEXT,
            item_code TEXT,
            raw_description_pattern TEXT NOT NULL,
            normalized_product_name TEXT,
            category TEXT,
            cost_type TEXT,
            default_unit TEXT,
            package_size TEXT,
            mapping_status TEXT NOT NULL DEFAULT 'NEEDS_REVIEW',
            confidence REAL DEFAULT 0.0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            times_seen INTEGER NOT NULL DEFAULT 1,
            approved_by TEXT,
            approved_at TEXT,
            UNIQUE(vendor_name, item_code)
        );

        CREATE TABLE IF NOT EXISTS chemical_invoice_extraction_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id INTEGER,
            pdf_sha256 TEXT,
            extraction_mode TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            raw_output TEXT
        );
        """
    )
    conn.commit()


# ─── Import service after patching ───────────────────────────────────────────
import services.chemical_invoices as chem
from services.chemical_invoices import (
    match_normalization_rule,
    normalize_line,
    reconcile,
    pdf_sha256,
    _stub_extraction,
    ingest_pdf_bytes,
    invoice_summary,
    list_invoices,
    mark_invoice_reviewed,
    list_aliases,
    approve_alias,
    update_alias,
)
import main as main_module
main_module.WEBHOOK_SECRET = "test-secret"


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def mem_conn():
    """Each test gets its own fresh in-memory DB."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _create_chem_tables(conn)
    # Override db() for the duration of this test
    original = db_module.db
    original_chem_db = chem.db
    db_module.db = lambda: _conn_factory(conn)
    chem.db = lambda: _conn_factory(conn)
    yield conn
    db_module.db = original
    chem.db = original_chem_db
    conn.close()


def _conn_factory(existing: sqlite3.Connection):
    """Return the same connection that the test already opened."""
    return _SharedConn(existing)


class _SharedConn:
    """Wraps an existing sqlite3.Connection so close() is a no-op (prevents test teardown issues)."""
    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, name):
        return getattr(self._c, name)

    def close(self):
        pass  # the fixture owns the connection lifetime


def _fake_pdf(content: str = "stub") -> bytes:
    return f"%PDF-1.4 {content}".encode()


class _FakeRequest:
    def __init__(self, body=None, *, secret: str = "test-secret", json_exc: Exception | None = None):
        self.headers = {"X-NTPP-Secret": secret}
        self._body = body
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._body


# ─── Normalization dictionary tests ──────────────────────────────────────────

class TestNormalizationRules:
    def test_calcium_hypochlorite_match(self):
        rule = match_normalization_rule("POOL BREEZE GRANULAR 68 CAL HYPO HAZMAT 100 LB")
        assert rule is not None
        assert rule["normalized_product_name"] == "calcium hypochlorite"
        assert rule["category"] == "shock"
        assert rule["cost_type"] == "chemical"

    def test_chlorine_tablets_match(self):
        rule = match_normalization_rule('POOL BREEZE 3" CHLORINATING TABLETS HAZMAT 50 LB')
        assert rule is not None
        assert rule["normalized_product_name"] == "chlorine tablets"

    def test_muriatic_acid_match(self):
        rule = match_normalization_rule("HASA MURIATIC ACID DISPOSABLE 4 X 1 GAL")
        assert rule is not None
        assert rule["normalized_product_name"] == "muriatic acid"
        assert rule["cost_type"] == "chemical"

    def test_sodium_bicarbonate_match(self):
        rule = match_normalization_rule("SODIUM BICARBONATE NSP POOL GRADE 50 LB")
        assert rule is not None
        assert rule["normalized_product_name"] == "sodium bicarbonate"

    def test_pool_salt_match(self):
        rule = match_normalization_rule("AQUASALT POOL SALT BAG 40 LB")
        assert rule is not None
        assert rule["normalized_product_name"] == "pool salt"

    def test_phosphate_remover_match(self):
        rule = match_normalization_rule("GLB PHOSFIGHT PLUS PHOSPHATE REMOVER 1 QUART")
        assert rule is not None
        assert rule["normalized_product_name"] == "phosphate remover"

    def test_algaecide_match(self):
        rule = match_normalization_rule("GLB STRIKE-OUT ALGAECIDE 1 QUART")
        assert rule is not None
        assert rule["normalized_product_name"] == "algaecide"

    def test_de_match(self):
        rule = match_normalization_rule("DIATOMACEOUS EARTH 25 LB")
        assert rule is not None
        assert rule["normalized_product_name"] == "diatomaceous earth"

    def test_testing_reagent_match(self):
        rule = match_normalization_rule("TAYLOR THIOSULF #7 N/10 DROPPER BOTTLE 2 OZ")
        assert rule is not None
        assert rule["normalized_product_name"] == "testing reagent"
        assert rule["cost_type"] == "testing"

    def test_pool_brush_match(self):
        rule = match_normalization_rule("PENTAIR BLENDED STAINLESS/NYLON BRUSH #907 WHITE BRISTLES 18\"")
        assert rule is not None
        assert rule["normalized_product_name"] == "pool brush"
        assert rule["cost_type"] == "equipment"

    def test_unknown_returns_none(self):
        rule = match_normalization_rule("WIDGET WIDGET MYSTERY PRODUCT XYZ")
        assert rule is None

    def test_case_insensitive(self):
        rule = match_normalization_rule("pool breeze granular 68 cal hypo 100 lb")
        assert rule is not None
        assert rule["normalized_product_name"] == "calcium hypochlorite"


# ─── normalize_line tests ─────────────────────────────────────────────────────

class TestNormalizeLine:
    def test_known_product_gets_dictionary_match(self, mem_conn):
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="PBZ88478",
            raw_description="PBZ88478 POOL BREEZE GRANULAR 68 CAL HYPO HAZMAT 100 LB",
        )
        assert result["normalized_product_name"] == "calcium hypochlorite"
        assert result["category"] == "shock"
        assert result["cost_type"] == "chemical"
        assert result["confidence"] >= 0.95
        assert result["alias_id"] is not None

    def test_unknown_product_fallback(self, mem_conn):
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="XYZ99999",
            raw_description="XYZ99999 MYSTERY WIDGET ULTRA PLUS",
        )
        assert result["normalized_product_name"] == "unknown"
        assert result["category"] == "other"
        assert result["cost_type"] == "misc"
        assert result["confidence"] < 0.80

    def test_ai_suggestion_high_confidence(self, mem_conn):
        ai = {
            "normalized_product_name": "stabilizer",
            "category": "stabilizer",
            "cost_type": "chemical",
            "default_unit": "lb",
            "confidence": 0.95,
        }
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="CYA001",
            raw_description="CYA001 CYANURIC ACID 8LB JUG",
            ai_suggestion=ai,
        )
        assert result["normalized_product_name"] == "stabilizer"
        assert result["category"] == "stabilizer"
        assert result["confidence"] == 0.95

        # Alias should be recorded as AI_SUGGESTED
        alias = mem_conn.execute(
            "SELECT mapping_status FROM chemical_product_aliases WHERE item_code='CYA001'"
        ).fetchone()
        assert alias["mapping_status"] == "AI_SUGGESTED"

    def test_ai_suggestion_low_confidence_falls_back_to_other(self, mem_conn):
        ai = {
            "normalized_product_name": "maybe clarifier",
            "category": "clarifier",
            "cost_type": "chemical",
            "default_unit": "quart",
            "confidence": 0.55,
        }
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="CLR001",
            raw_description="CLR001 SOMETHING OR OTHER",
            ai_suggestion=ai,
        )
        assert result["category"] == "other"
        assert result["cost_type"] == "misc"

        alias = mem_conn.execute(
            "SELECT mapping_status FROM chemical_product_aliases WHERE item_code='CLR001'"
        ).fetchone()
        assert alias["mapping_status"] == "NEEDS_REVIEW"

    def test_unknown_product_auto_ai_high_confidence_becomes_ai_suggested(self, mem_conn, monkeypatch):
        monkeypatch.setattr(chem, "CHEM_INVOICE_AI_CLASSIFICATION_ENABLED", True)
        monkeypatch.setattr(
            chem,
            "classify_product_with_ai",
            lambda *_args, **_kwargs: {
                "normalized_product_name": "clarifier",
                "category": "clarifier",
                "cost_type": "chemical",
                "confidence": 0.95,
            },
        )
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="AUTO001",
            raw_description="AUTO001 ULTRA WATER POLISH",
            allow_ai_classification=True,
            openai_api_key="fake-key",
        )
        assert result["normalized_product_name"] == "clarifier"
        assert result["category"] == "clarifier"
        assert result["confidence"] == 0.95
        alias = mem_conn.execute(
            "SELECT mapping_status FROM chemical_product_aliases WHERE item_code='AUTO001'"
        ).fetchone()
        assert alias["mapping_status"] == "AI_SUGGESTED"

    def test_unknown_product_auto_ai_low_confidence_stays_needs_review(self, mem_conn, monkeypatch):
        monkeypatch.setattr(chem, "CHEM_INVOICE_AI_CLASSIFICATION_ENABLED", True)
        monkeypatch.setattr(
            chem,
            "classify_product_with_ai",
            lambda *_args, **_kwargs: {
                "normalized_product_name": "maybe clarifier",
                "category": "clarifier",
                "cost_type": "chemical",
                "confidence": 0.40,
            },
        )
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="AUTO002",
            raw_description="AUTO002 ULTRA WATER POLISH",
            allow_ai_classification=True,
            openai_api_key="fake-key",
        )
        assert result["normalized_product_name"] == "unknown"
        assert result["category"] == "other"
        alias = mem_conn.execute(
            "SELECT mapping_status FROM chemical_product_aliases WHERE item_code='AUTO002'"
        ).fetchone()
        assert alias["mapping_status"] == "NEEDS_REVIEW"

    def test_unknown_product_ai_disabled_falls_back_to_needs_review(self, mem_conn, monkeypatch):
        monkeypatch.setattr(chem, "CHEM_INVOICE_AI_CLASSIFICATION_ENABLED", False)
        called = {"value": False}

        def _fake_classifier(*_args, **_kwargs):
            called["value"] = True
            return {
                "normalized_product_name": "clarifier",
                "category": "clarifier",
                "cost_type": "chemical",
                "confidence": 0.95,
            }

        monkeypatch.setattr(chem, "classify_product_with_ai", _fake_classifier)
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="AUTO003",
            raw_description="AUTO003 ULTRA WATER POLISH",
            allow_ai_classification=False,
            openai_api_key="fake-key",
        )
        assert result["normalized_product_name"] == "unknown"
        assert called["value"] is False
        alias = mem_conn.execute(
            "SELECT mapping_status FROM chemical_product_aliases WHERE item_code='AUTO003'"
        ).fetchone()
        assert alias["mapping_status"] == "NEEDS_REVIEW"

    def test_approved_alias_takes_priority_over_dictionary(self, mem_conn):
        # Insert a manual APPROVED override for a product the dictionary would classify differently
        now = "2026-01-01T00:00:00"
        mem_conn.execute(
            """
            INSERT INTO chemical_product_aliases
                (vendor_name, item_code, raw_description_pattern, normalized_product_name,
                 category, cost_type, default_unit, mapping_status, confidence,
                 first_seen_at, last_seen_at, times_seen)
            VALUES ('Heritage Pool Supply', 'PBZ88478', 'PBZ88478', 'custom shock product',
                    'custom_cat', 'chemical', 'lb', 'APPROVED', 1.0, ?, ?, 1)
            """,
            (now, now),
        )
        mem_conn.commit()

        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="PBZ88478",
            raw_description="PBZ88478 POOL BREEZE GRANULAR 68 CAL HYPO HAZMAT 100 LB",
        )
        # APPROVED alias should win over the dictionary
        assert result["normalized_product_name"] == "custom shock product"
        assert result["category"] == "custom_cat"

    def test_approved_alias_not_overwritten_by_auto_ai(self, mem_conn, monkeypatch):
        now = "2026-01-01T00:00:00"
        mem_conn.execute(
            """
            INSERT INTO chemical_product_aliases
                (vendor_name, item_code, raw_description_pattern, normalized_product_name,
                 category, cost_type, default_unit, mapping_status, confidence,
                 first_seen_at, last_seen_at, times_seen)
            VALUES ('Heritage Pool Supply', 'AUTO004', 'AUTO004', 'manual product',
                    'custom', 'chemical', 'lb', 'APPROVED', 1.0, ?, ?, 1)
            """,
            (now, now),
        )
        mem_conn.commit()

        called = {"value": False}

        def _fake_classifier(*_args, **_kwargs):
            called["value"] = True
            return {
                "normalized_product_name": "clarifier",
                "category": "clarifier",
                "cost_type": "chemical",
                "confidence": 0.95,
            }

        monkeypatch.setattr(chem, "classify_product_with_ai", _fake_classifier)
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="AUTO004",
            raw_description="AUTO004 SOMETHING CUSTOM",
            allow_ai_classification=True,
            openai_api_key="fake-key",
        )
        assert result["normalized_product_name"] == "manual product"
        assert called["value"] is False


# ─── Reconciliation tests ─────────────────────────────────────────────────────

class TestReconcile:
    def _items(self, *prices):
        return [{"extended_price": p} for p in prices]

    def test_reconciled_when_math_matches(self):
        result = reconcile(
            line_items=self._items(205.00, 145.00, 41.26, 22.07, 38.90),
            subtotal=452.23,
            tax=37.32,
            fees=0.0,
            invoice_total=489.55,
        )
        assert result["status"] == "RECONCILED"
        assert result["reconciliation"]["matches_subtotal"] is True
        assert result["reconciliation"]["matches_invoice_total"] is True

    def test_review_needed_when_subtotal_wrong(self):
        result = reconcile(
            line_items=self._items(205.00, 145.00),  # line_sum=350.00
            subtotal=999.99,                          # mismatch
            tax=37.32,
            fees=0.0,
            invoice_total=1037.31,
        )
        assert result["status"] == "REVIEW_NEEDED"
        assert result["reconciliation"]["matches_subtotal"] is False

    def test_review_needed_when_total_wrong(self):
        result = reconcile(
            line_items=self._items(205.00, 145.00),  # line_sum=350.00
            subtotal=350.00,                          # matches
            tax=37.32,
            fees=0.0,
            invoice_total=999.99,                     # calc=387.32, mismatch
        )
        assert result["status"] == "REVIEW_NEEDED"
        assert result["reconciliation"]["matches_invoice_total"] is False

    def test_tolerance_allows_penny_rounding(self):
        # line_sum will be 452.22 due to floating point; subtotal is 452.23
        result = reconcile(
            line_items=self._items(205.00, 145.00, 41.26, 22.07, 38.89),
            subtotal=452.22,
            tax=37.32,
            fees=0.0,
            invoice_total=489.54,
            tolerance=0.02,
        )
        assert result["status"] == "RECONCILED"

    def test_exceeded_tolerance_triggers_review(self):
        result = reconcile(
            line_items=self._items(205.00),
            subtotal=205.10,   # delta = 0.10 > 0.02
            tax=0.0,
            fees=0.0,
            invoice_total=205.10,
            tolerance=0.02,
        )
        assert result["status"] == "REVIEW_NEEDED"

    def test_exact_tolerance_boundary_is_allowed(self):
        result = reconcile(
            line_items=self._items(205.00),
            subtotal=205.02,
            tax=0.0,
            fees=0.0,
            invoice_total=205.02,
            tolerance=0.02,
        )
        assert result["status"] == "RECONCILED"

    def test_missing_invoice_totals_trigger_review(self):
        result = reconcile(
            line_items=self._items(0.0),
            subtotal=None,
            tax=0.0,
            fees=0.0,
            invoice_total=None,
            tolerance=0.02,
        )
        assert result["status"] == "REVIEW_NEEDED"
        assert result["reconciliation"]["has_subtotal"] is False
        assert result["reconciliation"]["has_invoice_total"] is False

    def test_reconciliation_dict_has_expected_keys(self):
        result = reconcile(
            line_items=self._items(100.00),
            subtotal=100.00, tax=8.25, fees=0.0, invoice_total=108.25,
        )
        keys = {"line_sum", "subtotal", "tax", "fees", "calculated_total",
                "invoice_total", "tolerance", "matches_subtotal", "matches_invoice_total",
                "has_subtotal", "has_invoice_total"}
        assert keys.issubset(result["reconciliation"].keys())


# ─── Full ingest pipeline (stub mode) ─────────────────────────────────────────

class TestIngestPipeline:
    def _do_ingest(self, mem_conn, extra_override=None):
        extracted = _stub_extraction()
        if extra_override:
            extracted.update(extra_override)
        return ingest_pdf_bytes(
            pdf_bytes=_fake_pdf(),
            mode="stub",
            _extracted_override=extracted,
        )

    def test_stub_extraction_fixture_is_valid(self):
        ext = _stub_extraction()
        assert ext["vendor_name"] == "Heritage Pool Supply"
        assert ext["invoice_total"] == 489.55
        assert ext["subtotal"] == 452.23
        assert len(ext["line_items"]) == 5

    def test_full_ingest_stub_returns_reconciled(self, mem_conn):
        result = self._do_ingest(mem_conn)
        assert result["status"] == "RECONCILED"
        assert result["line_count"] == 5
        assert result["invoice_id"] is not None

    def test_duplicate_pdf_sha256_blocked(self, mem_conn):
        pdf = _fake_pdf("same-content")
        # First ingest succeeds
        r1 = ingest_pdf_bytes(pdf, mode="stub", _extracted_override=_stub_extraction())
        assert r1["status"] == "RECONCILED"
        # Second ingest with identical bytes is rejected
        ext2 = _stub_extraction()
        ext2["invoice_number"] = "DIFFERENT-NUMBER"
        r2 = ingest_pdf_bytes(pdf, mode="stub", _extracted_override=ext2)
        assert r2["status"] == "DUPLICATE"

    def test_duplicate_vendor_invoice_number_blocked(self, mem_conn):
        # Two PDFs with different bytes but same vendor+invoice_number
        r1 = ingest_pdf_bytes(_fake_pdf("pdf-a"), mode="stub", _extracted_override=_stub_extraction())
        assert r1["status"] == "RECONCILED"

        ext2 = _stub_extraction()  # same vendor + invoice_number as stub fixture
        r2 = ingest_pdf_bytes(_fake_pdf("pdf-b"), mode="stub", _extracted_override=ext2)
        assert r2["status"] == "DUPLICATE"

    def test_missing_invoice_number_forces_review_needed(self, mem_conn):
        ext = _stub_extraction()
        ext["invoice_number"] = None
        result = ingest_pdf_bytes(_fake_pdf("missing-invoice-number"), mode="stub", _extracted_override=ext)
        assert result["status"] == "REVIEW_NEEDED"
        assert result["reconciliation"]["missing_invoice_number"] is True

    def test_reconciliation_mismatch_marks_review_needed(self, mem_conn):
        ext = _stub_extraction()
        ext["invoice_total"] = 9999.99  # force mismatch
        result = ingest_pdf_bytes(_fake_pdf("mismatch"), mode="stub", _extracted_override=ext)
        assert result["status"] == "REVIEW_NEEDED"

    def test_line_items_written_to_db(self, mem_conn):
        result = self._do_ingest(mem_conn)
        inv_id = result["invoice_id"]
        lines = mem_conn.execute(
            "SELECT * FROM chemical_invoice_lines WHERE invoice_id=? ORDER BY line_number",
            (inv_id,),
        ).fetchall()
        assert len(lines) == 5
        # Calcium hypochlorite line (item 1)
        assert lines[0]["normalized_product_name"] == "calcium hypochlorite"
        assert lines[0]["category"] == "shock"
        assert lines[0]["cost_type"] == "chemical"
        # Pool brush line (item 5) — equipment, not chemical
        assert lines[4]["cost_type"] == "equipment"

    def test_extraction_run_recorded(self, mem_conn):
        result = self._do_ingest(mem_conn)
        runs = mem_conn.execute(
            "SELECT * FROM chemical_invoice_extraction_runs WHERE invoice_id=?",
            (result["invoice_id"],),
        ).fetchall()
        assert len(runs) == 1
        assert runs[0]["success"] == 1
        assert runs[0]["extraction_mode"] == "stub"


# ─── Summary aggregation ──────────────────────────────────────────────────────

class TestSummary:
    def _ingest_stub(self, mem_conn, invoice_number: str, pdf_seed: str, invoice_date: str = "2026-04-23"):
        ext = _stub_extraction()
        ext["invoice_number"] = invoice_number
        ext["invoice_date"] = invoice_date
        return ingest_pdf_bytes(_fake_pdf(pdf_seed), mode="stub", _extracted_override=ext)

    def test_summary_totals(self, mem_conn):
        self._ingest_stub(mem_conn, "INV-001", "a")
        self._ingest_stub(mem_conn, "INV-002", "b")
        summary = invoice_summary(None, None)

        assert summary["invoice_count"] == 2
        assert summary["reconciled_count"] == 2
        # total_spend = 2 × 489.55
        assert abs(summary["total_spend"] - 979.10) < 0.05

    def test_summary_by_category(self, mem_conn):
        self._ingest_stub(mem_conn, "INV-001", "a")
        summary = invoice_summary(None, None)
        cats = summary["by_category"]
        assert "shock" in cats
        assert "tabs" in cats
        assert cats["shock"]["spend"] == 205.00

    def test_summary_by_cost_type(self, mem_conn):
        self._ingest_stub(mem_conn, "INV-001", "a")
        summary = invoice_summary(None, None)
        ct = summary["by_cost_type"]
        assert "chemical" in ct
        assert "equipment" in ct
        assert "testing" not in ct or ct["testing"]["spend"] >= 0

    def test_summary_by_vendor(self, mem_conn):
        self._ingest_stub(mem_conn, "INV-001", "a")
        summary = invoice_summary(None, None)
        assert "Heritage Pool Supply" in summary["by_vendor"]

    def test_summary_date_filter(self, mem_conn):
        self._ingest_stub(mem_conn, "INV-APR", "a", invoice_date="2026-04-23")
        self._ingest_stub(mem_conn, "INV-MAY", "b", invoice_date="2026-05-01")
        # Only April invoice should appear
        summary = invoice_summary("2026-04-01", "2026-04-30")
        assert summary["invoice_count"] == 1

    def test_chemical_spend_excludes_equipment(self, mem_conn):
        self._ingest_stub(mem_conn, "INV-001", "a")
        summary = invoice_summary(None, None)
        # Stub has 1 brush (equipment, $38.90) and 1 testing reagents ($16.38)
        # chemical_spend should be total minus those
        assert summary["chemical_spend"] < summary["total_spend"]
        assert summary["chemical_spend"] > 0


# ─── Mark reviewed ───────────────────────────────────────────────────────────

class TestMarkReviewed:
    def test_mark_reviewed_updates_status(self, mem_conn):
        ext = _stub_extraction()
        ext["invoice_total"] = 9999.99  # force REVIEW_NEEDED
        result = ingest_pdf_bytes(_fake_pdf("rev-test"), mode="stub", _extracted_override=ext)
        assert result["status"] == "REVIEW_NEEDED"

        ok = mark_invoice_reviewed(result["invoice_id"], reviewed_by="kevin")
        assert ok is True

        row = mem_conn.execute(
            "SELECT status, reviewed_by FROM chemical_invoices WHERE id=?",
            (result["invoice_id"],),
        ).fetchone()
        assert row["status"] == "REVIEWED"
        assert row["reviewed_by"] == "kevin"

    def test_mark_reviewed_nonexistent_returns_false(self, mem_conn):
        ok = mark_invoice_reviewed(99999)
        assert ok is False


# ─── Alias management ─────────────────────────────────────────────────────────

class TestAliasManagement:
    def test_approve_alias(self, mem_conn):
        # Ingest an unknown product to create a NEEDS_REVIEW alias
        ext = _stub_extraction()
        ext["invoice_number"] = "ALIAS-TEST"
        ext["line_items"] = [
            {
                "line_number": 1,
                "item_code": "UNK001",
                "raw_description": "UNK001 MYSTERY PRODUCT SUPER ULTRA",
                "quantity": 1.0,
                "unit": "ea",
                "unit_price": 50.00,
                "extended_price": 50.00,
            }
        ]
        ext["subtotal"] = 50.00
        ext["tax"] = 4.13
        ext["invoice_total"] = 54.13
        ingest_pdf_bytes(_fake_pdf("alias-test"), mode="stub", _extracted_override=ext)

        aliases = list_aliases(status="NEEDS_REVIEW")
        assert len(aliases) >= 1
        alias_id = next(a["id"] for a in aliases if a["item_code"] == "UNK001")

        ok = approve_alias(
            alias_id,
            approved_by="kevin",
            updates={"normalized_product_name": "mystery chemical", "category": "clarifier", "cost_type": "chemical"},
        )
        assert ok is True

        row = mem_conn.execute(
            "SELECT mapping_status, normalized_product_name, approved_by FROM chemical_product_aliases WHERE id=?",
            (alias_id,),
        ).fetchone()
        assert row["mapping_status"] == "APPROVED"
        assert row["normalized_product_name"] == "mystery chemical"
        assert row["approved_by"] == "kevin"

    def test_update_alias(self, mem_conn):
        now = "2026-01-01T00:00:00"
        mem_conn.execute(
            """
            INSERT INTO chemical_product_aliases
                (vendor_name, item_code, raw_description_pattern, normalized_product_name,
                 category, cost_type, default_unit, mapping_status, confidence,
                 first_seen_at, last_seen_at, times_seen)
            VALUES ('Test Vendor', 'TST001', 'TST001 WIDGET', 'widget',
                    'other', 'misc', 'each', 'NEEDS_REVIEW', 0.0, ?, ?, 1)
            """,
            (now, now),
        )
        mem_conn.commit()
        alias_id = mem_conn.execute(
            "SELECT id FROM chemical_product_aliases WHERE item_code='TST001'"
        ).fetchone()["id"]

        ok = update_alias(alias_id, {"category": "equipment_part", "cost_type": "equipment"})
        assert ok is True

        row = mem_conn.execute(
            "SELECT category, cost_type FROM chemical_product_aliases WHERE id=?",
            (alias_id,),
        ).fetchone()
        assert row["category"] == "equipment_part"
        assert row["cost_type"] == "equipment"

    def test_invalid_alias_update_rejected(self, mem_conn):
        now = "2026-01-01T00:00:00"
        mem_conn.execute(
            """
            INSERT INTO chemical_product_aliases
                (vendor_name, item_code, raw_description_pattern, normalized_product_name,
                 category, cost_type, default_unit, mapping_status, confidence,
                 first_seen_at, last_seen_at, times_seen)
            VALUES ('Test Vendor', 'TST002', 'TST002 WIDGET', 'widget',
                    'other', 'misc', 'each', 'NEEDS_REVIEW', 0.0, ?, ?, 1)
            """,
            (now, now),
        )
        mem_conn.commit()
        alias_id = mem_conn.execute(
            "SELECT id FROM chemical_product_aliases WHERE item_code='TST002'"
        ).fetchone()["id"]

        with pytest.raises(ValueError):
            update_alias(alias_id, {"cost_type": "not-real"})

    def test_approved_alias_not_downgraded_on_reingest(self, mem_conn):
        """An APPROVED alias should never be overwritten by the normalization pipeline."""
        now = "2026-01-01T00:00:00"
        mem_conn.execute(
            """
            INSERT INTO chemical_product_aliases
                (vendor_name, item_code, raw_description_pattern, normalized_product_name,
                 category, cost_type, default_unit, mapping_status, confidence,
                 first_seen_at, last_seen_at, times_seen)
            VALUES ('Heritage Pool Supply', 'PBZ88478', 'PBZ88478',
                    'admin override', 'custom', 'chemical', 'lb',
                    'APPROVED', 1.0, ?, ?, 5)
            """,
            (now, now),
        )
        mem_conn.commit()

        # Normalize should respect the APPROVED alias
        result = normalize_line(
            mem_conn,
            vendor_name="Heritage Pool Supply",
            item_code="PBZ88478",
            raw_description="PBZ88478 POOL BREEZE GRANULAR 68 CAL HYPO HAZMAT 100 LB",
        )
        assert result["normalized_product_name"] == "admin override"
        assert result["category"] == "custom"

        # Alias must still be APPROVED
        row = mem_conn.execute(
            "SELECT mapping_status FROM chemical_product_aliases WHERE item_code='PBZ88478'"
        ).fetchone()
        assert row["mapping_status"] == "APPROVED"


class TestScanInbox:
    def test_safe_filename_and_collision_suffix(self, mem_conn, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        failed = tmp_path / "failed"
        inbox.mkdir()

        pdf_a = inbox / "A weird invoice!!.pdf"
        pdf_b = inbox / "A weird invoice copy!!.pdf"
        pdf_a.write_bytes(_fake_pdf("same-pdf"))
        pdf_b.write_bytes(_fake_pdf("same-pdf"))

        result = chem.scan_inbox(str(inbox), str(processed), str(failed), mode="stub")

        assert result["scanned"] == 2
        processed_names = sorted(p.name for p in processed.iterdir())
        assert processed_names[0].startswith("heritage_pool_supply_0000000000_stub_")
        assert processed_names[1].startswith("heritage_pool_supply_0000000000_stub_")
        assert processed_names[0] != processed_names[1]
        assert not any(".." in name for name in processed_names)

    def test_oversized_pdf_rejected_and_moved_to_failed(self, mem_conn, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        failed = tmp_path / "failed"
        inbox.mkdir()
        pdf_path = inbox / "large.pdf"
        pdf_path.write_bytes(b"%PDF-1.4 " + b"x" * 64)

        result = chem.scan_inbox(
            str(inbox),
            str(processed),
            str(failed),
            mode="stub",
            max_pdf_bytes=16,
        )

        assert result["counts"]["PDF_TOO_LARGE"] == 1
        assert not processed.exists() or not list(processed.iterdir())
        failed_names = [p.name for p in failed.iterdir()]
        assert len(failed_names) == 1
        assert failed_names[0].endswith(".pdf")

    def test_extraction_failure_moves_to_failed(self, mem_conn, tmp_path):
        inbox = tmp_path / "inbox"
        processed = tmp_path / "processed"
        failed = tmp_path / "failed"
        inbox.mkdir()
        pdf_path = inbox / "broken.pdf"
        pdf_path.write_bytes(_fake_pdf("broken"))

        original_extract = chem.extract_pdf_text
        chem.extract_pdf_text = lambda _: (_ for _ in ()).throw(RuntimeError("bad pdf"))
        try:
            result = chem.scan_inbox(str(inbox), str(processed), str(failed), mode="text")
        finally:
            chem.extract_pdf_text = original_extract

        assert result["counts"]["EXTRACTION_FAILED"] == 1
        assert len(list(failed.iterdir())) == 1
        assert mem_conn.execute("SELECT COUNT(*) AS c FROM chemical_invoices").fetchone()["c"] == 0


class TestChemicalInvoiceEndpoints:
    def test_mark_reviewed_rejects_malformed_json(self):
        request = _FakeRequest(json_exc=ValueError("bad json"))
        with pytest.raises(main_module.HTTPException) as exc:
            asyncio.run(main_module.chemical_invoices_mark_reviewed(request, 1))
        assert exc.value.status_code == 400

    def test_alias_approve_rejects_malformed_json(self):
        request = _FakeRequest(json_exc=ValueError("bad json"))
        with pytest.raises(main_module.HTTPException) as exc:
            asyncio.run(main_module.chemical_aliases_approve(request, 1))
        assert exc.value.status_code == 400

    def test_invalid_invoice_status_rejected(self):
        request = _FakeRequest()
        with pytest.raises(main_module.HTTPException) as exc:
            main_module.chemical_invoices_list(request, status="NOT_A_STATUS")
        assert exc.value.status_code == 400

    def test_invalid_alias_status_rejected(self):
        request = _FakeRequest()
        with pytest.raises(main_module.HTTPException) as exc:
            main_module.chemical_aliases_list(request, status="NOT_A_STATUS")
        assert exc.value.status_code == 400

    def test_invalid_summary_date_rejected(self):
        request = _FakeRequest()
        with pytest.raises(main_module.HTTPException) as exc:
            main_module.chemical_invoices_summary(request, start_date="2026-13-01")
        assert exc.value.status_code == 400
