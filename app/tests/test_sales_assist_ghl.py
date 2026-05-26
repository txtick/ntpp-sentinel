import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.sales_assist as sales_assist


def test_ghl_contact_url_requires_contact_and_location(monkeypatch):
    monkeypatch.delenv("GHL_LOCATION_ID", raising=False)

    assert sales_assist._ghl_contact_url("contact-1") is None


def test_ghl_contact_url_uses_location_contact_route(monkeypatch):
    monkeypatch.setenv("GHL_LOCATION_ID", "loc 1")
    monkeypatch.setattr(sales_assist, "GHL_APP_BASE_URL", "https://app.gohighlevel.com")
    monkeypatch.setattr(sales_assist, "GHL_CONTACT_URL_TEMPLATE", "")

    url = sales_assist._ghl_contact_url("contact/abc")

    assert url == "https://app.gohighlevel.com/v2/location/loc%201/contacts/detail/contact%2Fabc"


def test_ghl_contact_url_template_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GHL_LOCATION_ID", "loc-1")
    monkeypatch.setattr(
        sales_assist,
        "GHL_CONTACT_URL_TEMPLATE",
        "https://example.test/l/{location_id}/c/{contact_id}",
    )

    assert sales_assist._ghl_contact_url("contact-1") == "https://example.test/l/loc-1/c/contact-1"


def test_sales_assist_frontend_prefers_ghl_open_action():
    frontend = (Path(__file__).resolve().parents[2] / "web-frontend" / "app.js").read_text()

    assert "Open in GHL" in frontend
    assert "Call from device" in frontend


def test_sales_assist_priority_ignores_expiration_fields():
    base_score, base_reasons = sales_assist._compute_priority(
        {"status": "Sent", "total_amount": 100},
        [],
    )
    expiring_score, expiring_reasons = sales_assist._compute_priority(
        {"status": "Sent", "total_amount": 100, "expiration_date": "2026-05-26T00:00:00+00:00"},
        [],
    )
    expired_status_score, expired_status_reasons = sales_assist._compute_priority(
        {"status": "Expired", "total_amount": 100, "expiration_date": "2026-01-01T00:00:00+00:00"},
        [],
    )

    assert expiring_score == base_score
    assert expiring_reasons == base_reasons
    assert expired_status_score == 10
    assert not any("expir" in reason.lower() for reason in expired_status_reasons)
