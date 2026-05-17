import os
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

import services.route_sandbox as rs


def _scenario_detail(assignments):
    groups = {}
    for item in assignments:
        key = f"{item.get('source_account_id')}|{item.get('day_of_week')}"
        groups.setdefault(
            key,
            {
                "source_account_id": item.get("source_account_id"),
                "tech_name": item.get("tech_name") or item.get("source_account_id") or "",
                "day_of_week": item.get("day_of_week"),
                "warnings": [],
                "stops": [],
            },
        )["stops"].append(item)
    return {
        "scenario": {"id": 10, "name": "Test Scenario", "status": "draft"},
        "route_groups": list(groups.values()),
    }


def test_validate_scenario_blocks_duplicate_pool_assignments(monkeypatch):
    monkeypatch.setattr(rs, "_require_postgres", lambda: None)
    monkeypatch.setattr(
        rs,
        "get_scenario",
        lambda _scenario_id: _scenario_detail(
            [
                {
                    "source_service_location_id": "pool-1",
                    "source_account_id": "tech-a",
                    "tech_name": "Tech A",
                    "day_of_week": "Monday",
                    "customer_name": "Alpha",
                },
                {
                    "source_service_location_id": "pool-1",
                    "source_account_id": "tech-b",
                    "tech_name": "Tech B",
                    "day_of_week": "Tuesday",
                    "customer_name": "Alpha",
                },
            ]
        ),
    )

    result = rs.validate_scenario(10)

    assert result["valid"] is False
    assert any("appears in multiple route groups" in e["message"] for e in result["errors"])


def test_validate_scenario_blocks_missing_tech_and_invalid_day(monkeypatch):
    monkeypatch.setattr(rs, "_require_postgres", lambda: None)
    monkeypatch.setattr(
        rs,
        "get_scenario",
        lambda _scenario_id: _scenario_detail(
            [
                {
                    "source_service_location_id": "pool-2",
                    "source_account_id": "",
                    "tech_name": "",
                    "day_of_week": "Funday",
                    "customer_name": "Beta",
                }
            ]
        ),
    )

    result = rs.validate_scenario(10)

    assert result["valid"] is False
    messages = " ".join(e["message"] for e in result["errors"])
    assert "missing a technician" in messages
    assert "invalid day" in messages


def test_route_metrics_separate_commute_from_stop_to_stop_miles():
    stops = [
        {"latitude": 33.0, "longitude": -96.8, "estimated_duration_minutes": 30, "stop_order": 10},
        {"latitude": 33.1, "longitude": -96.8, "estimated_duration_minutes": 30, "stop_order": 20},
    ]
    far_home_profile = {
        "home_latitude": 35.0,
        "home_longitude": -99.0,
        "default_start_location_type": "home",
        "default_end_location_type": "home",
        "include_start_drive_in_metrics": False,
        "include_end_drive_in_metrics": False,
    }

    metrics = rs._build_route_metrics(stops, far_home_profile)

    assert metrics["start_to_first_stop_miles"] is not None
    assert metrics["last_stop_to_end_miles"] is not None
    assert metrics["total_weighted_miles"] == metrics["total_without_start_end_miles"]
    assert metrics["total_with_start_end_miles"] > metrics["total_without_start_end_miles"]


def test_missing_coordinates_do_not_break_route_metrics():
    metrics = rs._build_route_metrics(
        [
            {"latitude": None, "longitude": None, "estimated_duration_minutes": 45, "stop_order": 10},
            {"latitude": 33.1, "longitude": -96.8, "estimated_duration_minutes": 45, "stop_order": 20},
        ]
    )

    assert metrics["missing_coords_count"] == 1
    assert metrics["stop_to_stop_miles"] == 0


class _ScenarioCursor:
    def __init__(self, status):
        self.status = status

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return {"id": 10, "status": self.status, "updated_at": "now", "name": "Scenario"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ScenarioConn:
    def __init__(self, status):
        self.status = status

    def cursor(self):
        return _ScenarioCursor(self.status)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_manual_packet_requires_approved_valid_scenario(monkeypatch):
    monkeypatch.setattr(rs, "_require_postgres", lambda: None)
    monkeypatch.setattr(rs, "validate_scenario", lambda _id: {"valid": True, "errors": []})
    monkeypatch.setattr(rs, "pg", lambda: _ScenarioConn("draft"))

    with pytest.raises(HTTPException) as exc:
        rs._require_approved_valid_scenario_for_packet(10)

    assert exc.value.status_code == 409
    assert "approved" in str(exc.value.detail)


def test_route_sandbox_has_no_skimmer_writeback_endpoint_or_adapter():
    backend = Path(__file__).resolve().parents[1] / "web_backend_main.py"
    service = Path(__file__).resolve().parents[1] / "services" / "route_sandbox.py"
    text = backend.read_text() + "\n" + service.read_text()

    forbidden = [
        "push-to-skimmer",
        "push_to_skimmer",
        "/push",
        "PUT /Routes",
        "POST /Routes",
        "UPDATE sk_route_assignment",
        "UPDATE sk_route_stop",
    ]
    lowered = text.lower()
    for token in forbidden:
        assert token.lower() not in lowered


def test_frontend_uses_manual_packet_language_not_push_to_skimmer():
    frontend = Path(__file__).resolve().parents[2] / "web-frontend" / "app.js"
    text = frontend.read_text()

    assert "Generate Manual Update Packet" in text
    assert "Push to Skimmer" not in text
    assert "push-to-Skimmer" not in text
