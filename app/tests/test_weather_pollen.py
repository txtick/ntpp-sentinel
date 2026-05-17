import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

import web_backend_main as wb


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_urlopen(req, timeout=10):
    url = getattr(req, "full_url", "")
    if "open-meteo.com/v1/forecast" in url:
        return _FakeResponse(
            {
                "current": {
                    "temperature_2m": 61,
                    "apparent_temperature": 57,
                    "weather_code": 3,
                    "wind_speed_10m": 12,
                    "precipitation": 0.0,
                    "uv_index": 0.55,
                },
                "daily": {
                    "time": [
                        "2026-04-30",
                        "2026-05-01",
                        "2026-05-02",
                        "2026-05-03",
                        "2026-05-04",
                        "2026-05-05",
                        "2026-05-06",
                    ],
                    "temperature_2m_max": [70, 71, 72, 73, 74, 75, 76],
                    "temperature_2m_min": [50, 51, 52, 53, 54, 55, 56],
                    "weather_code": [1, 2, 3, 4, 5, 6, 7],
                    "precipitation_sum": [0, 0, 0, 0, 0, 0, 0],
                    "wind_speed_10m_max": [10, 11, 12, 13, 14, 15, 16],
                },
            }
        )
    if "air-quality-api.open-meteo.com" in url:
        return _FakeResponse(
            {
                "hourly": {
                    "time": [
                        "2026-04-30T00:00",
                        "2026-05-01T00:00",
                        "2026-05-02T00:00",
                        "2026-05-03T00:00",
                        "2026-05-04T00:00",
                        "2026-05-05T00:00",
                        "2026-05-06T00:00",
                    ],
                    "dust": [10, 11, 12, 13, 14, 15, 16],
                }
            }
        )
    raise AssertionError(f"Unexpected URL {url}")


def test_fetch_current_pollen_with_retry_succeeds_after_retries(monkeypatch):
    attempts = {"count": 0}

    def _fake_fetch():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("temporary failure")
        return {"tree_risk": "High"}

    monkeypatch.setattr(wb, "_fetch_current_pollen", _fake_fetch)
    monkeypatch.setattr(wb.time, "sleep", lambda _s: None)

    result = wb._fetch_current_pollen_with_retry(attempts=3, delay_seconds=0)

    assert result == {"tree_risk": "High"}
    assert attempts["count"] == 3


def test_fetch_current_pollen_sends_documented_ambee_headers(monkeypatch):
    captured = {}

    def _fake_ambee_urlopen(req, timeout=10):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResponse(
            {
                "data": [
                    {
                        "Risk": {
                            "tree_pollen": "Low",
                            "grass_pollen": "Moderate",
                            "weed_pollen": "High",
                        },
                        "Count": {
                            "tree_pollen": 1,
                            "grass_pollen": 2,
                            "weed_pollen": 3,
                        },
                        "Species": {"Tree": {"Oak": 1}, "Weed": {"Ragweed": 3}},
                        "updatedAt": "2026-05-16T12:00:00Z",
                    }
                ]
            }
        )

    import urllib.request as _urllib_req

    monkeypatch.setattr(wb, "AMBEE_API_KEY", "test-ambee-key")
    monkeypatch.setattr(_urllib_req, "urlopen", _fake_ambee_urlopen)

    result = wb._fetch_current_pollen()

    assert "latest/pollen/by-lat-lng" in captured["url"]
    assert captured["headers"]["X-api-key"] == "test-ambee-key"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["Content-type"] == "application/json"
    assert result["tree_risk"] == "Low"
    assert result["ragweed_count"] == 3


def test_api_weather_uses_todays_stored_pollen_when_live_fetch_fails(monkeypatch):
    monkeypatch.setattr(wb, "_dashboard_read_auth_or_401", lambda _request: None)
    monkeypatch.setattr(wb, "_weather_cache", {})
    monkeypatch.setattr(wb, "_weather_cache_ts", 0.0)
    monkeypatch.setattr(wb, "AMBEE_API_KEY", "configured")
    monkeypatch.setattr(wb, "_local_weather_date_str", lambda: "2026-05-06")
    monkeypatch.setattr(wb, "_fetch_current_pollen_with_retry", lambda: (_ for _ in ()).throw(RuntimeError("ambee down")))
    monkeypatch.setattr(wb, "upsert_pollen_daily_log", lambda _pollen: None)
    monkeypatch.setattr(wb, "get_avg_water_temp", lambda days=7: 71.6)
    monkeypatch.setattr(
        wb,
        "get_pollen_daily_log",
        lambda days=7: {
            "2026-05-06": {
                "tree_risk": "Moderate",
                "grass_risk": "Low",
                "weed_risk": "Low",
                "tree_count": 12,
                "grass_count": 0,
                "weed_count": 0,
                "tree_detail": "Oak 12",
            }
        },
    )

    import urllib.request as _urllib_req

    monkeypatch.setattr(_urllib_req, "urlopen", _fake_urlopen)

    result = wb.api_weather(object())

    assert result["current_pollen"]["tree_risk"] == "Moderate"
    assert result["current_pollen"]["tree_detail"] == "Oak 12"
    assert result["environmental"][-1]["tree_risk"] == "Moderate"
